from __future__ import annotations

import math
import os
import time
import uuid
from typing import Any, Literal

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from kalshi_client import (
    API_HOST,
    Credentials,
    KalshiAuthError,
    KalshiClient,
    KalshiError,
    SERIES_BY_ASSET,
    verify_credentials,
)
from risk import check_trade, validate_settings
from store import StoreError, StoredConnection, VaultStore

APP_NAME = "FreeHoliday Kalshi API"
DB_PATH = os.getenv("FREEHOLIDAY_DB", "./data/freeholiday.db")
FERNET_KEY = os.getenv("FREEHOLIDAY_FERNET_KEY", "")
KALSHI_HOST = os.getenv("KALSHI_API_HOST", API_HOST)
CONNECTION_TTL_HOURS = int(os.getenv("FREEHOLIDAY_CONNECTION_TTL_HOURS", "12"))
ALLOWED_ORIGINS = [
    x.strip()
    for x in os.getenv(
        "FREEHOLIDAY_ALLOWED_ORIGINS",
        "https://godthefather1952.github.io,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if x.strip()
]

store = VaultStore(DB_PATH, FERNET_KEY)
app = FastAPI(title=APP_NAME, version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


class Forecast(BaseModel):
    asset: Literal["BTC", "ETH", "DOGE", "NEAR"]
    direction: Literal["Higher", "Lower"]
    predicted_price: float = Field(gt=0)
    confidence: float = Field(ge=0.5, le=1.0)
    window_start: int
    window_end: int
    locked_price: float | None = Field(default=None, gt=0)
    forecast_error_sigma_pct: float | None = Field(default=None, gt=0)
    forecast_error_bias_pct: float | None = None
    forecast_error_samples: int = Field(default=0, ge=0)


class OpportunityRequest(BaseModel):
    forecasts: list[Forecast]


class DecisionRequest(BaseModel):
    asset: Literal["BTC", "ETH", "DOGE", "NEAR"]
    window_start: int
    window_end: int
    decision: Literal["confirm", "skip"]
    market_ticker: str
    suggested_outcome: Literal["YES", "NO"]
    predicted_price: float
    threshold: float
    confidence: float
    forecast_direction: Literal["Higher", "Lower"]
    locked_price: float | None = Field(default=None, gt=0)
    forecast_error_sigma_pct: float | None = Field(default=None, gt=0)
    forecast_error_bias_pct: float | None = None
    forecast_error_samples: int = Field(default=0, ge=0)


class OrderRequest(BaseModel):
    asset: Literal["BTC", "ETH", "DOGE", "NEAR"]
    window_start: int
    amount_dollars: float | None = Field(default=None, ge=0.10)


class SettingsRequest(BaseModel):
    auto_trade: bool = False
    trade_size_dollars: float = Field(default=0.10, ge=0.10)
    max_trade_dollars: float = Field(default=5.0, ge=0.10)
    max_daily_exposure_dollars: float = Field(default=25.0, ge=0.10)
    max_daily_loss_dollars: float = Field(default=10.0, ge=0.10)
    max_open_positions: int = Field(default=4, ge=1)
    allowed_assets: list[Literal["BTC", "ETH", "DOGE", "NEAR"]] = [
        "BTC",
        "ETH",
        "DOGE",
        "NEAR",
    ]


def bearer_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401, detail="Missing Kalshi connection token"
        )
    return authorization.split(" ", 1)[1].strip()


def connection(token: str = Depends(bearer_token)) -> StoredConnection:
    try:
        return store.get_connection(token)
    except StoreError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def client_for(conn: StoredConnection) -> KalshiClient:
    return KalshiClient(
        Credentials(conn.key_id, conn.private_key_pem), host=KALSHI_HOST
    )


def dollars_from_balance(payload: dict[str, Any]) -> float | None:
    raw = payload.get("balance")
    if isinstance(raw, (int, float)):
        return float(raw) / 100.0
    raw = payload.get("balance_dollars")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _nonzero(value: Any) -> bool:
    try:
        return abs(float(value)) > 1e-12
    except (TypeError, ValueError):
        return False


def count_open_positions(payload: dict[str, Any], *, freeholiday_only: bool = False) -> int:
    rows = payload.get("market_positions") or payload.get("positions") or []
    prefixes = tuple(f"{series}-" for series in SERIES_BY_ASSET.values())
    count = 0
    for row in rows:
        if freeholiday_only:
            ticker = str(row.get("ticker") or row.get("market_ticker") or "")
            if not ticker.startswith(prefixes):
                continue
        values = [
            row.get("position_fp"),
            row.get("position"),
        ]
        if any(_nonzero(v) for v in values):
            count += 1
    return count


def estimated_taker_fee(price: float, contracts: float) -> float:
    if contracts <= 0 or not (0.0 < price < 1.0):
        return 0.0
    raw = 0.07 * contracts * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0


def kalshi_expiration_value(payload: dict[str, Any]) -> float | None:
    market = payload.get("market") if isinstance(payload.get("market"), dict) else payload
    raw = market.get("expiration_value")
    if raw is None:
        return None
    try:
        value = float(str(raw).replace("$", "").replace(",", "").strip())
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def settled_outcome(payload: dict[str, Any]) -> str | None:
    market = payload.get("market") if isinstance(payload.get("market"), dict) else payload
    raw = str(
        market.get("result")
        or market.get("settlement_result")
        or market.get("outcome")
        or ""
    ).strip().lower()
    if raw in ("yes", "up", "1", "true"):
        return "YES"
    if raw in ("no", "down", "0", "false"):
        return "NO"
    return None


async def sync_trade_settlements(conn: StoredConnection, kalshi: KalshiClient) -> None:
    for trade in store.unsettled_trades(conn.id, 50):
        try:
            payload = await kalshi.market(str(trade["market_ticker"]))
        except KalshiError:
            continue
        outcome = settled_outcome(payload)
        if not outcome:
            continue
        close_price = kalshi_expiration_value(payload)
        contracts = float(trade["contracts"])
        price = float(trade["contract_price"])
        fee = estimated_taker_fee(price, contracts)
        won = outcome == str(trade["outcome"]).upper()
        pnl = contracts * (1.0 - price) - fee if won else -(contracts * price) - fee
        store.settle_trade(
            conn.id,
            int(trade["id"]),
            status="WIN" if won else "LOSS",
            pnl=pnl,
            kalshi_close_price=close_price,
        )


COINBASE_PRODUCT_BY_ASSET = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "DOGE": "DOGE-USD",
    "NEAR": "NEAR-USD",
}

# Conservative 15-minute fallback residual volatility when there are not yet
# enough local settled forecasts to estimate the model's own error distribution.
ENTRY_FALLBACK_SIGMA = {
    "BTC": 0.0035,
    "ETH": 0.0050,
    "DOGE": 0.0090,
    "NEAR": 0.0080,
}


async def coinbase_spot(asset: str) -> float:
    product = COINBASE_PRODUCT_BY_ASSET.get(asset.upper())
    if not product:
        raise KalshiError(f"Unsupported spot asset: {asset}")
    url = f"https://api.exchange.coinbase.com/products/{product}/ticker"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                url,
                headers={"Accept": "application/json"},
            )
        response.raise_for_status()
        value = float(response.json().get("price"))
    except Exception as exc:
        raise KalshiError(
            f"Could not read live {asset} price for entry analysis"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise KalshiError(f"Live {asset} price is invalid")
    return value


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return 0.0
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def kalshi_entry_calibration(
    connection_id: str,
    asset: str,
) -> dict[str, Any]:
    samples = store.settled_entry_samples(connection_id, asset, 120)
    residuals: list[float] = []
    for sample in samples:
        try:
            actual = float(sample["kalshi_close_price"])
            predicted = float(sample["predicted_price"])
        except (TypeError, ValueError):
            continue
        if actual <= 0 or predicted <= 0:
            continue
        residual = math.log(actual / predicted)
        if math.isfinite(residual):
            residuals.append(residual)

    if not residuals:
        return {"samples": 0, "bias": 0.0, "sigma": None}

    bias = _median(residuals)
    deviations = [abs(x - bias) for x in residuals]
    mad_sigma = 1.4826 * _median(deviations)
    rms_sigma = math.sqrt(
        sum((x - bias) ** 2 for x in residuals) / len(residuals)
    )
    sigma = max(mad_sigma, rms_sigma, 0.0001)
    return {
        "samples": len(residuals),
        "bias": bias,
        "sigma": sigma,
    }


def entry_engine_analysis(
    forecast: Forecast,
    quote,
    *,
    current_price: float,
    calibration: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    total_seconds = 15.0 * 60.0
    market_close_ts = float(getattr(quote, "close_ts", 0.0) or 0.0)
    if market_close_ts > 0:
        remaining_seconds = _clamp(
            market_close_ts - now,
            0.0,
            total_seconds,
        )
    else:
        remaining_seconds = _clamp(
            (float(forecast.window_end) / 1000.0) - now,
            0.0,
            total_seconds,
        )
    remaining_fraction = remaining_seconds / total_seconds

    predicted_price = float(forecast.predicted_price)
    locked_price = (
        float(forecast.locked_price)
        if forecast.locked_price
        else None
    )
    if locked_price and locked_price > 0:
        full_expected_log_move = math.log(
            predicted_price / locked_price
        )
        remaining_expected_log_move = (
            full_expected_log_move * remaining_fraction
        )
        live_entry_forecast = current_price * math.exp(
            remaining_expected_log_move
        )
    else:
        live_entry_forecast = math.exp(
            math.log(current_price)
            + remaining_fraction
            * (math.log(predicted_price) - math.log(current_price))
        )
        full_expected_log_move = math.log(
            predicted_price / current_price
        )
        remaining_expected_log_move = (
            full_expected_log_move * remaining_fraction
        )

    calibration = calibration or {}
    calibration_samples = int(calibration.get("samples") or 0)
    calibration_sigma = calibration.get("sigma")
    calibration_bias = float(calibration.get("bias") or 0.0)

    supplied_sigma = forecast.forecast_error_sigma_pct
    supplied_bias = float(forecast.forecast_error_bias_pct or 0.0)
    fallback_sigma = ENTRY_FALLBACK_SIGMA[forecast.asset]

    if (
        calibration_samples >= 5
        and calibration_sigma is not None
        and math.isfinite(float(calibration_sigma))
        and float(calibration_sigma) > 0
    ):
        sigma_full = float(calibration_sigma)
        bias_full = calibration_bias
        error_source = "Kalshi settled history"
        error_samples = calibration_samples
    elif (
        supplied_sigma is not None
        and math.isfinite(float(supplied_sigma))
        and float(supplied_sigma) > 0
        and forecast.forecast_error_samples >= 5
    ):
        sigma_full = float(supplied_sigma)
        bias_full = supplied_bias
        error_source = "local settled forecast errors"
        error_samples = int(forecast.forecast_error_samples)
    else:
        sigma_full = max(
            fallback_sigma,
            abs(full_expected_log_move) * 0.75,
        )
        bias_full = 0.0
        error_source = "asset fallback"
        error_samples = 0

    # Apply the historically observed full-window bias only to the
    # remaining fraction of the current window.
    live_entry_forecast *= math.exp(
        bias_full * remaining_fraction
    )

    uncertainty_fraction = max(
        remaining_fraction,
        30.0 / total_seconds,
    )
    sigma_remaining = max(
        sigma_full * math.sqrt(uncertainty_fraction),
        0.00005,
    )

    threshold = float(quote.threshold)
    z = math.log(live_entry_forecast / threshold) / sigma_remaining
    probability_yes = _clamp(_normal_cdf(z), 0.001, 0.999)
    suggested_outcome = "YES" if probability_yes >= 0.5 else "NO"
    side_probability = (
        probability_yes
        if suggested_outcome == "YES"
        else 1.0 - probability_yes
    )
    contract_price = quote.outcome_price(suggested_outcome)
    market_implied = (
        float(contract_price)
        if contract_price is not None
        else None
    )
    fee_adjusted_breakeven = None
    model_edge = None
    if market_implied is not None:
        fee_per_contract = (
            0.07 * market_implied * (1.0 - market_implied)
        )
        fee_adjusted_breakeven = _clamp(
            market_implied + fee_per_contract,
            0.001,
            0.999,
        )
        model_edge = side_probability - fee_adjusted_breakeven

    distance_dollars = threshold - current_price
    distance_pct = (threshold / current_price) - 1.0

    return {
        "current_price": current_price,
        "live_entry_forecast": live_entry_forecast,
        "remaining_seconds": int(round(remaining_seconds)),
        "remaining_fraction": remaining_fraction,
        "expected_remaining_move_pct": math.exp(
            remaining_expected_log_move
        ) - 1.0,
        "error_bias_pct": bias_full,
        "error_sigma_pct": sigma_full,
        "remaining_error_sigma_pct": sigma_remaining,
        "error_source": error_source,
        "error_samples": error_samples,
        "distance_to_target": distance_dollars,
        "distance_to_target_pct": distance_pct,
        "probability_yes": probability_yes,
        "target_probability": side_probability,
        "suggested_outcome": suggested_outcome,
        "contract_price": contract_price,
        "market_implied_probability": market_implied,
        "fee_adjusted_breakeven": fee_adjusted_breakeven,
        "model_edge": model_edge,
    }

def opportunity_payload(
    forecast: Forecast,
    quote,
    confirmation: dict[str, Any] | None,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    return {
        "asset": forecast.asset,
        "window_start": forecast.window_start,
        "window_end": forecast.window_end,
        "forecast_direction": forecast.direction,
        "confidence": forecast.confidence,
        "predicted_price": forecast.predicted_price,
        "locked_price": forecast.locked_price,
        "forecast_error_sigma_pct": forecast.forecast_error_sigma_pct,
        "forecast_error_samples": forecast.forecast_error_samples,
        "market_ticker": quote.market_ticker,
        "event_ticker": quote.event_ticker,
        "market_title": quote.title,
        "threshold": quote.threshold,
        "suggested_outcome": analysis["suggested_outcome"],
        "contract_price": analysis["contract_price"],
        "yes_price": quote.yes_ask,
        "no_price": quote.no_ask,
        "market_open_ts": quote.open_ts,
        "market_close_ts": quote.close_ts,
        "current_price": analysis["current_price"],
        "live_entry_forecast": analysis["live_entry_forecast"],
        "time_remaining_seconds": analysis["remaining_seconds"],
        "distance_to_target": analysis["distance_to_target"],
        "distance_to_target_pct": analysis["distance_to_target_pct"],
        "target_probability": analysis["target_probability"],
        "probability_yes": analysis["probability_yes"],
        "market_implied_probability": analysis[
            "market_implied_probability"
        ],
        "fee_adjusted_breakeven": analysis[
            "fee_adjusted_breakeven"
        ],
        "model_edge": analysis["model_edge"],
        "expected_remaining_move_pct": analysis[
            "expected_remaining_move_pct"
        ],
        "error_bias_pct": analysis["error_bias_pct"],
        "error_sigma_pct": analysis["error_sigma_pct"],
        "remaining_error_sigma_pct": analysis[
            "remaining_error_sigma_pct"
        ],
        "error_source": analysis["error_source"],
        "error_samples": analysis["error_samples"],
        "decision": confirmation.get("decision") if confirmation else None,
    }


async def execute_confirmed_trade(
    conn: StoredConnection,
    *,
    asset: str,
    window_start: int,
    amount_dollars: float,
) -> dict[str, Any]:
    confirmation = store.confirmation(conn.id, asset, window_start)
    if not confirmation or confirmation["decision"] != "confirm":
        raise HTTPException(
            status_code=409,
            detail="This coin/window has not been confirmed",
        )
    payload = confirmation["payload"]
    kalshi = client_for(conn)
    try:
        quote = await kalshi.current_market(asset)
    except (KalshiError, KalshiAuthError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if quote.market_ticker != confirmation["market_ticker"]:
        raise HTTPException(
            status_code=409,
            detail=(
                "Kalshi rolled to a different 15-minute market; "
                "reconfirm this coin"
            ),
        )

    outcome = str(payload["suggested_outcome"]).upper()
    current_price = quote.outcome_price(outcome)
    if current_price is None:
        raise HTTPException(
            status_code=409,
            detail="No executable Kalshi quote is available",
        )

    await sync_trade_settlements(conn, kalshi)
    settings = store.get_settings(conn.id)
    try:
        positions = await kalshi.positions()
    except (KalshiError, KalshiAuthError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not verify positions before trading: {exc}",
        ) from exc

    risk = check_trade(
        settings=settings,
        asset=asset,
        amount_dollars=amount_dollars,
        contract_price=current_price,
        daily_exposure=store.daily_exposure(conn.id),
        realized_pnl=store.daily_realized_pnl(conn.id),
        open_positions=count_open_positions(positions, freeholiday_only=True),
        paused=conn.paused,
    )
    if not risk.ok:
        raise HTTPException(status_code=409, detail=risk.reason)

    contracts = math.floor((amount_dollars / current_price) * 100.0) / 100.0
    if contracts < 0.01:
        raise HTTPException(
            status_code=409,
            detail="Trade amount is too small for the current Kalshi price",
        )
    actual_spend = round(contracts * current_price, 6)
    if actual_spend > float(settings["max_trade_dollars"]) + 1e-9:
        raise HTTPException(
            status_code=409,
            detail="Calculated order exceeds the per-trade limit",
        )

    client_order_id = (
        f"fh-{asset.lower()}-{window_start}-{uuid.uuid4().hex[:10]}"
    )
    try:
        result = await kalshi.create_order_v2(
            ticker=quote.market_ticker,
            outcome=outcome,
            contracts=contracts,
            outcome_price=current_price,
            client_order_id=client_order_id,
        )
    except (KalshiError, KalshiAuthError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    order = (
        result.get("order")
        if isinstance(result.get("order"), dict)
        else result
    )
    status = str(order.get("status") or "submitted")
    order_id = str(order.get("order_id") or "") or None
    trade = {
        "asset": asset,
        "window_start": window_start,
        "market_ticker": quote.market_ticker,
        "outcome": outcome,
        "requested_dollars": actual_spend,
        "contract_price": current_price,
        "contracts": contracts,
        "client_order_id": client_order_id,
        "order_id": order_id,
        "status": status,
        "raw": result,
    }
    store.add_trade(conn.id, trade)
    return trade


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": APP_NAME,
        "time": int(time.time()),
    }


@app.post("/api/kalshi/connect")
async def connect_kalshi(
    api_key_id: str = Form(...),
    private_key: UploadFile = File(...),
):
    data = await private_key.read()
    if len(data) > 64_000:
        raise HTTPException(
            status_code=400,
            detail="PEM file is unexpectedly large",
        )
    try:
        pem = data.decode("utf-8").strip() + "\n"
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="PEM file must be UTF-8 text",
        ) from exc
    creds = Credentials(api_key_id.strip(), pem)
    try:
        verified = await verify_credentials(creds, host=KALSHI_HOST)
    except (KalshiAuthError, KalshiError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    connection_id, token = store.create_connection(
        creds.key_id,
        creds.private_key_pem,
        ttl_hours=CONNECTION_TTL_HOURS,
    )
    return {
        "connected": True,
        "connection_id": connection_id,
        "connection_token": token,
        "expires_in_hours": CONNECTION_TTL_HOURS,
        "balance_dollars": verified["balance_dollars"],
    }


@app.get("/api/kalshi/account")
async def account(conn: StoredConnection = Depends(connection)):
    kalshi = client_for(conn)
    try:
        await sync_trade_settlements(conn, kalshi)
        balance = await kalshi.balance()
        positions = await kalshi.positions()
    except (KalshiAuthError, KalshiError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "connected": True,
        "paused": conn.paused,
        "expires_at": conn.expires_at,
        "balance_dollars": dollars_from_balance(balance),
        "open_positions": count_open_positions(positions, freeholiday_only=True),
        "kalshi_open_positions": count_open_positions(positions),
        "today_pnl": store.daily_realized_pnl(conn.id),
        "settings": store.get_settings(conn.id),
    }


@app.post("/api/kalshi/opportunities")
async def opportunities(
    req: OpportunityRequest,
    conn: StoredConnection = Depends(connection),
):
    kalshi = client_for(conn)
    out = []
    for forecast in req.forecasts:
        try:
            quote = await kalshi.current_market(forecast.asset)
            current_price = await coinbase_spot(forecast.asset)
            calibration = kalshi_entry_calibration(
                conn.id,
                forecast.asset,
            )
            analysis = entry_engine_analysis(
                forecast,
                quote,
                current_price=current_price,
                calibration=calibration,
            )
            confirmation = store.confirmation(
                conn.id,
                forecast.asset,
                forecast.window_start,
            )
            out.append(
                opportunity_payload(
                    forecast,
                    quote,
                    confirmation,
                    analysis,
                )
            )
        except (KalshiError, KalshiAuthError) as exc:
            out.append(
                {
                    "asset": forecast.asset,
                    "window_start": forecast.window_start,
                    "error": str(exc),
                }
            )
    return {"opportunities": out}


@app.post("/api/kalshi/decision")
async def decision(
    req: DecisionRequest,
    conn: StoredConnection = Depends(connection),
):
    kalshi = client_for(conn)
    try:
        quote = await kalshi.current_market(req.asset)
    except (KalshiError, KalshiAuthError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if quote.market_ticker != req.market_ticker:
        raise HTTPException(
            status_code=409,
            detail="Kalshi market changed; refresh before confirming",
        )

    forecast = Forecast(
        asset=req.asset,
        direction=req.forecast_direction,
        predicted_price=req.predicted_price,
        confidence=req.confidence,
        window_start=req.window_start,
        window_end=req.window_end,
        locked_price=req.locked_price,
        forecast_error_sigma_pct=req.forecast_error_sigma_pct,
        forecast_error_bias_pct=req.forecast_error_bias_pct,
        forecast_error_samples=req.forecast_error_samples,
    )
    try:
        current_price = await coinbase_spot(req.asset)
    except KalshiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    calibration = kalshi_entry_calibration(
        conn.id,
        req.asset,
    )
    analysis = entry_engine_analysis(
        forecast,
        quote,
        current_price=current_price,
        calibration=calibration,
    )
    suggested = str(analysis["suggested_outcome"])
    if suggested != req.suggested_outcome:
        raise HTTPException(
            status_code=409,
            detail=(
                "Entry side changed with the live price; "
                "refresh before confirming"
            ),
        )

    payload = {
        "asset": req.asset,
        "window_start": req.window_start,
        "window_end": req.window_end,
        "market_ticker": quote.market_ticker,
        "suggested_outcome": suggested,
        "predicted_price": req.predicted_price,
        "locked_price": req.locked_price,
        "threshold": quote.threshold,
        "confidence": req.confidence,
        "forecast_direction": req.forecast_direction,
        "forecast_error_sigma_pct": req.forecast_error_sigma_pct,
        "forecast_error_bias_pct": req.forecast_error_bias_pct,
        "forecast_error_samples": req.forecast_error_samples,
        "current_price_at_confirm": analysis["current_price"],
        "live_entry_forecast": analysis["live_entry_forecast"],
        "time_remaining_seconds": analysis["remaining_seconds"],
        "target_probability": analysis["target_probability"],
        "probability_yes": analysis["probability_yes"],
        "market_implied_probability": analysis[
            "market_implied_probability"
        ],
        "fee_adjusted_breakeven": analysis[
            "fee_adjusted_breakeven"
        ],
        "model_edge": analysis["model_edge"],
        "expected_remaining_move_pct": analysis[
            "expected_remaining_move_pct"
        ],
        "error_bias_pct": analysis["error_bias_pct"],
        "error_sigma_pct": analysis["error_sigma_pct"],
        "remaining_error_sigma_pct": analysis[
            "remaining_error_sigma_pct"
        ],
        "confirmed_at": int(time.time()),
    }
    store.save_confirmation(
        conn.id,
        asset=req.asset,
        window_start=req.window_start,
        market_ticker=quote.market_ticker,
        decision=req.decision,
        payload=payload,
    )

    if req.decision == "confirm":
        settings = store.get_settings(conn.id)
        if settings.get("auto_trade"):
            trade = await execute_confirmed_trade(
                conn,
                asset=req.asset,
                window_start=req.window_start,
                amount_dollars=float(
                    settings["trade_size_dollars"]
                ),
            )
            return {
                "decision": "confirm",
                "auto_trade": True,
                "trade": trade,
            }

    return {
        "decision": req.decision,
        "auto_trade": False,
    }


@app.post("/api/kalshi/orders")
async def place_order(
    req: OrderRequest,
    conn: StoredConnection = Depends(connection),
):
    settings = store.get_settings(conn.id)
    amount = float(
        req.amount_dollars or settings["trade_size_dollars"]
    )
    return await execute_confirmed_trade(
        conn,
        asset=req.asset,
        window_start=req.window_start,
        amount_dollars=amount,
    )


@app.get("/api/kalshi/settings")
async def get_settings(
    conn: StoredConnection = Depends(connection),
):
    return store.get_settings(conn.id)


@app.put("/api/kalshi/settings")
async def put_settings(
    req: SettingsRequest,
    conn: StoredConnection = Depends(connection),
):
    try:
        settings = validate_settings(req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return store.save_settings(conn.id, settings)


@app.get("/api/kalshi/trades")
async def trades(conn: StoredConnection = Depends(connection)):
    kalshi = client_for(conn)
    try:
        await sync_trade_settlements(conn, kalshi)
    except (KalshiError, KalshiAuthError):
        pass
    return {"trades": store.list_trades(conn.id, 50)}


@app.post("/api/kalshi/pause")
async def pause(conn: StoredConnection = Depends(connection)):
    store.set_paused(conn.id, True)
    return {"paused": True}


@app.post("/api/kalshi/resume")
async def resume(conn: StoredConnection = Depends(connection)):
    store.set_paused(conn.id, False)
    return {"paused": False}


@app.delete("/api/kalshi/disconnect")
async def disconnect(conn: StoredConnection = Depends(connection)):
    store.disconnect(conn.id)
    return {"connected": False}
