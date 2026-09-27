from __future__ import annotations

import base64
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

API_HOST = "https://api.elections.kalshi.com"
API_BASE = "/trade-api/v2"

SERIES_BY_ASSET = {
    "BTC": "KXBTC15M",
    "ETH": "KXETH15M",
    "DOGE": "KXDOGE15M",
    "NEAR": "KXNEAR15M",
}


class KalshiError(RuntimeError):
    pass


class KalshiAuthError(KalshiError):
    pass


@dataclass(slots=True)
class Credentials:
    key_id: str
    private_key_pem: str

    def validate(self) -> None:
        if not self.key_id.strip():
            raise KalshiAuthError("API key ID is required")
        if "PRIVATE KEY" not in self.private_key_pem:
            raise KalshiAuthError("PEM private key is invalid")


class Signer:
    def __init__(self, credentials: Credentials) -> None:
        credentials.validate()
        try:
            private = serialization.load_pem_private_key(
                credentials.private_key_pem.encode("utf-8"), password=None
            )
        except Exception as exc:
            raise KalshiAuthError(f"Could not load PEM private key: {exc}") from exc
        if not isinstance(private, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
            raise KalshiAuthError("Only RSA or Ed25519 Kalshi private keys are supported")
        self._key_id = credentials.key_id.strip()
        self._private = private

    def headers(self, method: str, full_path_without_query: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{full_path_without_query}".encode("utf-8")
        if isinstance(self._private, rsa.RSAPrivateKey):
            signature_bytes = self._private.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        else:
            signature_bytes = self._private.sign(message)
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature_bytes).decode("ascii"),
            "Content-Type": "application/json",
        }


def _iso_ts(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _levels(payload: dict[str, Any], side: str) -> list[tuple[float, float]]:
    book = payload.get("orderbook_fp") or payload.get("orderbook") or {}
    raw = book.get(f"{side}_dollars") or book.get(side) or []
    out: list[tuple[float, float]] = []
    for row in raw:
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 < price < 1 and qty > 0:
            out.append((price, qty))
    return out


@dataclass(slots=True)
class MarketQuote:
    asset: str
    series_ticker: str
    market_ticker: str
    event_ticker: str
    title: str
    threshold: float
    open_ts: float
    close_ts: float
    yes_ask: float | None
    no_ask: float | None

    def outcome_price(self, outcome: str) -> float | None:
        return self.yes_ask if outcome.upper() == "YES" else self.no_ask


class KalshiClient:
    def __init__(
        self,
        credentials: Credentials | None = None,
        *,
        host: str = API_HOST,
        timeout: float = 15.0,
    ) -> None:
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.signer = Signer(credentials) if credentials else None

    async def request(
        self,
        method: str,
        path: str,
        *,
        signed: bool = False,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = ""
        if params:
            flat = [(k, str(v)) for k, v in params.items() if v is not None]
            if flat:
                query = "?" + urlencode(flat)
        full_path = API_BASE + path
        headers: dict[str, str] = {"Accept": "application/json"}
        if signed:
            if not self.signer:
                raise KalshiAuthError("Authenticated Kalshi request requires credentials")
            headers.update(self.signer.headers(method, full_path))
        url = self.host + full_path + query
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            response = await client.request(method, url, headers=headers, json=body)
        if response.status_code in (401, 403):
            raise KalshiAuthError(
                f"Kalshi rejected the API credentials (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise KalshiError(
                f"Kalshi request failed (HTTP {response.status_code}): {detail}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise KalshiError("Kalshi returned a non-JSON response") from exc

    async def balance(self) -> dict[str, Any]:
        return await self.request("GET", "/portfolio/balance", signed=True)

    async def positions(self) -> dict[str, Any]:
        return await self.request(
            "GET",
            "/portfolio/positions",
            signed=True,
            params={"limit": 1000, "count_filter": "position"},
        )

    async def fills(self, limit: int = 100) -> dict[str, Any]:
        return await self.request(
            "GET", "/portfolio/fills", signed=True, params={"limit": limit}
        )

    async def orders(self, limit: int = 100) -> dict[str, Any]:
        return await self.request(
            "GET", "/portfolio/orders", signed=True, params={"limit": limit}
        )

    async def markets(
        self, *, series_ticker: str, status: str = "open", limit: int = 50
    ) -> dict[str, Any]:
        return await self.request(
            "GET",
            "/markets",
            params={
                "series_ticker": series_ticker,
                "status": status,
                "limit": limit,
            },
        )

    async def market(self, ticker: str) -> dict[str, Any]:
        return await self.request("GET", f"/markets/{ticker}")

    async def orderbook(self, ticker: str) -> dict[str, Any]:
        return await self.request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": 50}
        )

    async def current_market(
        self, asset: str, *, now: float | None = None
    ) -> MarketQuote:
        asset = asset.upper()
        series = SERIES_BY_ASSET.get(asset)
        if not series:
            raise KalshiError(f"Unsupported asset: {asset}")
        now = time.time() if now is None else now
        payload = await self.markets(
            series_ticker=series, status="open", limit=50
        )
        markets = payload.get("markets") or []
        candidates: list[dict[str, Any]] = []
        for raw in markets:
            open_ts = _iso_ts(raw.get("open_time"))
            close_ts = _iso_ts(raw.get("close_time"))
            status = str(raw.get("status") or "").lower()
            if status not in ("open", "active"):
                continue
            if open_ts and close_ts and open_ts <= now < close_ts:
                candidates.append(raw)
        if not candidates:
            for raw in markets:
                close_ts = _iso_ts(raw.get("close_time"))
                if close_ts > now and close_ts - now <= 20 * 60:
                    candidates.append(raw)
        if not candidates:
            raise KalshiError(f"No open {asset} 15-minute Kalshi market found")
        raw = min(
            candidates,
            key=lambda row: _iso_ts(row.get("close_time")) or float("inf"),
        )
        threshold = _number(raw.get("floor_strike"))
        if not threshold or threshold <= 0:
            raise KalshiError(
                "Kalshi target/threshold is not published yet for this window"
            )
        ticker = str(raw.get("ticker") or "")
        if not ticker:
            raise KalshiError("Kalshi market did not include a ticker")
        book = await self.orderbook(ticker)
        yes_levels = _levels(book, "yes")
        no_levels = _levels(book, "no")
        yes_bid = max((p for p, _ in yes_levels), default=None)
        no_bid = max((p for p, _ in no_levels), default=None)
        yes_ask = (
            round(1.0 - no_bid, 4)
            if no_bid is not None
            else _number(raw.get("yes_ask_dollars"))
        )
        no_ask = (
            round(1.0 - yes_bid, 4)
            if yes_bid is not None
            else _number(raw.get("no_ask_dollars"))
        )
        return MarketQuote(
            asset=asset,
            series_ticker=series,
            market_ticker=ticker,
            event_ticker=str(raw.get("event_ticker") or ""),
            title=str(raw.get("title") or f"{asset} 15 min"),
            threshold=threshold,
            open_ts=_iso_ts(raw.get("open_time")),
            close_ts=_iso_ts(raw.get("close_time")),
            yes_ask=yes_ask,
            no_ask=no_ask,
        )

    async def create_order_v2(
        self,
        *,
        ticker: str,
        outcome: str,
        contracts: float,
        outcome_price: float,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if contracts < 0.01:
            raise KalshiError("contracts must be at least 0.01")
        outcome = outcome.upper()
        if outcome not in ("YES", "NO"):
            raise KalshiError("outcome must be YES or NO")
        if not (0.0 < outcome_price < 1.0):
            raise KalshiError("contract price must be between $0 and $1")
        side = "bid" if outcome == "YES" else "ask"
        book_price = (
            outcome_price if outcome == "YES" else 1.0 - outcome_price
        )
        body = {
            "ticker": ticker,
            "side": side,
            "count": f"{contracts:.2f}",
            "price": f"{book_price:.4f}",
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "cancel_order_on_pause": True,
        }
        return await self.request(
            "POST", "/portfolio/events/orders", signed=True, body=body
        )


async def verify_credentials(
    credentials: Credentials, *, host: str = API_HOST
) -> dict[str, Any]:
    client = KalshiClient(credentials, host=host)
    balance = await client.balance()
    cents = _number(balance.get("balance"))
    if cents is None:
        dollars = _number(balance.get("balance_dollars"))
    else:
        dollars = cents / 100.0
    return {"balance_dollars": dollars, "raw": balance}
