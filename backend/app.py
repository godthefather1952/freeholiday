from __future__ import annotations

import math
import os
import time
import uuid
from typing import Any, Literal

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


class OpportunityRequest(BaseModel):
    forecasts: list[Forecast]


class DecisionRequest(BaseModel):
    asset: Literal["BTC", "ETH", "DOGE", "NEAR"]
    window_start: int
    decision: Literal["confirm", "skip"]
    market_ticker: str
    suggested_outcome: Literal["YES", "NO"]
    predicted_price: float
    threshold: float
    confidence: float
    forecast_direction: Literal["Higher", "Lower"]


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
    max_open_positions: int = Field(default=3, ge=1)
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
        )


def opportunity_payload(
    forecast: Forecast, quote, confirmation: dict[str, Any] | None
) -> dict[str, Any]:
    suggested = (
        "YES" if forecast.predicted_price >= quote.threshold else "NO"
    )
    price = quote.outcome_price(suggested)
    return {
        "asset": forecast.asset,
        "window_start": forecast.window_start,
        "window_end": forecast.window_end,
        "forecast_direction": forecast.direction,
        "confidence": forecast.confidence,
        "predicted_price": forecast.predicted_price,
        "market_ticker": quote.market_ticker,
        "event_ticker": quote.event_ticker,
        "market_title": quote.title,
        "threshold": quote.threshold,
        "suggested_outcome": suggested,
        "contract_price": price,
        "yes_price": quote.yes_ask,
        "no_price": quote.no_ask,
        "market_open_ts": quote.open_ts,
        "market_close_ts": quote.close_ts,
        "distance_to_threshold": forecast.predicted_price - quote.threshold,
        "distance_to_threshold_pct": (
            (forecast.predicted_price / quote.threshold) - 1.0
        )
        * 100.0,
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
            confirmation = store.confirmation(
                conn.id,
                forecast.asset,
                forecast.window_start,
            )
            out.append(
                opportunity_payload(forecast, quote, confirmation)
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

    suggested = (
        "YES" if req.predicted_price >= quote.threshold else "NO"
    )
    if suggested != req.suggested_outcome:
        raise HTTPException(
            status_code=409,
            detail="Suggested side changed; refresh before confirming",
        )

    payload = {
        "asset": req.asset,
        "window_start": req.window_start,
        "market_ticker": quote.market_ticker,
        "suggested_outcome": suggested,
        "predicted_price": req.predicted_price,
        "threshold": quote.threshold,
        "confidence": req.confidence,
        "forecast_direction": req.forecast_direction,
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
