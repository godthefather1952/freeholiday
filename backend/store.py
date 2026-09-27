from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class StoreError(RuntimeError):
    pass


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class StoredConnection:
    id: str
    key_id: str
    private_key_pem: str
    paused: bool
    expires_at: float


DEFAULT_SETTINGS = {
    "auto_trade": False,
    "trade_size_dollars": 0.10,
    "max_trade_dollars": 5.0,
    "max_daily_exposure_dollars": 25.0,
    "max_daily_loss_dollars": 10.0,
    "max_open_positions": 4,
    "allowed_assets": ["BTC", "ETH", "DOGE", "NEAR"],
}


class VaultStore:
    def __init__(self, db_path: str, fernet_key: str) -> None:
        if not fernet_key:
            raise StoreError("FREEHOLIDAY_FERNET_KEY is required")
        try:
            self.fernet = Fernet(fernet_key.encode("utf-8"))
        except Exception as exc:
            raise StoreError("FREEHOLIDAY_FERNET_KEY is not a valid Fernet key") from exc
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def conn(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def init_db(self) -> None:
        with self.conn() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS connections (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT UNIQUE NOT NULL,
                    key_id_enc BLOB NOT NULL,
                    pem_enc BLOB NOT NULL,
                    paused INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    connection_id TEXT PRIMARY KEY,
                    settings_json TEXT NOT NULL,
                    FOREIGN KEY(connection_id) REFERENCES connections(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS confirmations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_id TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    market_ticker TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(connection_id, asset, window_start),
                    FOREIGN KEY(connection_id) REFERENCES connections(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    connection_id TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    market_ticker TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    requested_dollars REAL NOT NULL,
                    contract_price REAL NOT NULL,
                    contracts REAL NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    order_id TEXT,
                    status TEXT NOT NULL,
                    gross_pnl REAL,
                    kalshi_close_price REAL,
                    raw_json TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(connection_id) REFERENCES connections(id) ON DELETE CASCADE
                );
                """
            )
            self._migrate_trades_allow_multiple(con)
            self._migrate_trade_analysis_columns(con)

    def _migrate_trades_allow_multiple(self, con: sqlite3.Connection) -> None:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        sql = str(row["sql"] or "") if row else ""
        normalized = " ".join(sql.split()).lower()
        if "unique(connection_id, asset, window_start)" not in normalized:
            return

        con.executescript(
            """
            CREATE TABLE trades_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id TEXT NOT NULL,
                asset TEXT NOT NULL,
                window_start INTEGER NOT NULL,
                market_ticker TEXT NOT NULL,
                outcome TEXT NOT NULL,
                requested_dollars REAL NOT NULL,
                contract_price REAL NOT NULL,
                contracts REAL NOT NULL,
                client_order_id TEXT NOT NULL UNIQUE,
                order_id TEXT,
                status TEXT NOT NULL,
                gross_pnl REAL,
                kalshi_close_price REAL,
                raw_json TEXT,
                created_at REAL NOT NULL,
                FOREIGN KEY(connection_id) REFERENCES connections(id) ON DELETE CASCADE
            );
            INSERT INTO trades_new(
                id,connection_id,asset,window_start,market_ticker,outcome,
                requested_dollars,contract_price,contracts,client_order_id,
                order_id,status,gross_pnl,raw_json,created_at
            )
            SELECT
                id,connection_id,asset,window_start,market_ticker,outcome,
                requested_dollars,contract_price,contracts,client_order_id,
                order_id,status,gross_pnl,raw_json,created_at
            FROM trades;
            DROP TABLE trades;
            ALTER TABLE trades_new RENAME TO trades;
            """
        )

    def _migrate_trade_analysis_columns(self, con: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in con.execute("PRAGMA table_info(trades)").fetchall()
        }
        if "kalshi_close_price" not in columns:
            con.execute("ALTER TABLE trades ADD COLUMN kalshi_close_price REAL")

    def _encrypt(self, value: str) -> bytes:
        return self.fernet.encrypt(value.encode("utf-8"))

    def _decrypt(self, value: bytes) -> str:
        try:
            return self.fernet.decrypt(value).decode("utf-8")
        except InvalidToken as exc:
            raise StoreError("Credential vault could not decrypt stored credentials") from exc

    def create_connection(self, key_id: str, private_key_pem: str, ttl_hours: int = 12) -> tuple[str, str]:
        connection_id = secrets.token_hex(16)
        token = secrets.token_urlsafe(40)
        now = time.time()
        expires = now + max(1, ttl_hours) * 3600
        with self.conn() as con:
            con.execute(
                "INSERT INTO connections(id,token_hash,key_id_enc,pem_enc,created_at,last_used_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    connection_id,
                    token_hash(token),
                    self._encrypt(key_id),
                    self._encrypt(private_key_pem),
                    now,
                    now,
                    expires,
                ),
            )
            con.execute(
                "INSERT INTO settings(connection_id,settings_json) VALUES(?,?)",
                (connection_id, json.dumps(DEFAULT_SETTINGS)),
            )
        return connection_id, token

    def get_connection(self, token: str) -> StoredConnection:
        if not token:
            raise StoreError("Missing connection token")
        with self.conn() as con:
            row = con.execute(
                "SELECT * FROM connections WHERE token_hash=?", (token_hash(token),)
            ).fetchone()
            if not row:
                raise StoreError("Kalshi connection not found")
            if float(row["expires_at"]) <= time.time():
                con.execute("DELETE FROM connections WHERE id=?", (row["id"],))
                raise StoreError("Kalshi connection expired; reconnect the account")
            con.execute(
                "UPDATE connections SET last_used_at=? WHERE id=?", (time.time(), row["id"])
            )
            return StoredConnection(
                id=str(row["id"]),
                key_id=self._decrypt(row["key_id_enc"]),
                private_key_pem=self._decrypt(row["pem_enc"]),
                paused=bool(row["paused"]),
                expires_at=float(row["expires_at"]),
            )

    def get_settings(self, connection_id: str) -> dict[str, Any]:
        with self.conn() as con:
            row = con.execute(
                "SELECT settings_json FROM settings WHERE connection_id=?", (connection_id,)
            ).fetchone()
        if not row:
            return dict(DEFAULT_SETTINGS)
        data = json.loads(row["settings_json"])
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data)
        # v2 default: four supported assets can be open together.
        if data.get("max_open_positions") == 3:
            merged["max_open_positions"] = 4
        return merged

    def save_settings(self, connection_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(settings)
        with self.conn() as con:
            con.execute(
                "INSERT INTO settings(connection_id,settings_json) VALUES(?,?) ON CONFLICT(connection_id) DO UPDATE SET settings_json=excluded.settings_json",
                (connection_id, json.dumps(merged)),
            )
        return merged

    def set_paused(self, connection_id: str, paused: bool) -> None:
        with self.conn() as con:
            con.execute(
                "UPDATE connections SET paused=? WHERE id=?", (1 if paused else 0, connection_id)
            )

    def save_confirmation(
        self,
        connection_id: str,
        *,
        asset: str,
        window_start: int,
        market_ticker: str,
        decision: str,
        payload: dict[str, Any],
    ) -> None:
        with self.conn() as con:
            con.execute(
                """
                INSERT INTO confirmations(connection_id,asset,window_start,market_ticker,decision,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(connection_id,asset,window_start)
                DO UPDATE SET market_ticker=excluded.market_ticker,decision=excluded.decision,payload_json=excluded.payload_json,created_at=excluded.created_at
                """,
                (connection_id, asset, window_start, market_ticker, decision, json.dumps(payload), time.time()),
            )

    def confirmation(self, connection_id: str, asset: str, window_start: int) -> dict[str, Any] | None:
        with self.conn() as con:
            row = con.execute(
                "SELECT * FROM confirmations WHERE connection_id=? AND asset=? AND window_start=?",
                (connection_id, asset, window_start),
            ).fetchone()
        if not row:
            return None
        return {
            "decision": row["decision"],
            "market_ticker": row["market_ticker"],
            "payload": json.loads(row["payload_json"]),
        }

    def existing_trade(self, connection_id: str, asset: str, window_start: int) -> dict[str, Any] | None:
        with self.conn() as con:
            row = con.execute(
                "SELECT * FROM trades WHERE connection_id=? AND asset=? AND window_start=?",
                (connection_id, asset, window_start),
            ).fetchone()
        return dict(row) if row else None

    def add_trade(self, connection_id: str, trade: dict[str, Any]) -> None:
        with self.conn() as con:
            con.execute(
                """
                INSERT INTO trades(connection_id,asset,window_start,market_ticker,outcome,requested_dollars,contract_price,contracts,client_order_id,order_id,status,kalshi_close_price,raw_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    connection_id,
                    trade["asset"],
                    trade["window_start"],
                    trade["market_ticker"],
                    trade["outcome"],
                    trade["requested_dollars"],
                    trade["contract_price"],
                    trade["contracts"],
                    trade["client_order_id"],
                    trade.get("order_id"),
                    trade.get("status", "submitted"),
                    trade.get("kalshi_close_price"),
                    json.dumps(trade.get("raw") or {}),
                    time.time(),
                ),
            )

    def list_trades(self, connection_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.conn() as con:
            rows = con.execute(
                """
                SELECT t.*, c.payload_json AS confirmation_payload
                FROM trades t
                LEFT JOIN confirmations c
                  ON c.connection_id=t.connection_id
                 AND c.asset=t.asset
                 AND c.window_start=t.window_start
                WHERE t.connection_id=?
                ORDER BY t.created_at DESC
                LIMIT ?
                """,
                (connection_id, max(1, min(limit, 200))),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_confirmation = item.pop("confirmation_payload", None)
            try:
                confirmation = json.loads(raw_confirmation) if raw_confirmation else {}
            except (TypeError, ValueError):
                confirmation = {}
            item["predicted_price"] = confirmation.get("predicted_price")
            item["threshold"] = confirmation.get("threshold")
            item["current_price_at_confirm"] = confirmation.get(
                "current_price_at_confirm"
            )
            item["live_entry_forecast"] = confirmation.get(
                "live_entry_forecast"
            )
            item["time_remaining_seconds"] = confirmation.get(
                "time_remaining_seconds"
            )
            item["target_probability"] = confirmation.get(
                "target_probability"
            )
            item["market_implied_probability"] = confirmation.get(
                "market_implied_probability"
            )
            item["model_edge"] = confirmation.get("model_edge")
            item["remaining_error_sigma_pct"] = confirmation.get(
                "remaining_error_sigma_pct"
            )
            out.append(item)
        return out

    def unsettled_trades(self, connection_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.conn() as con:
            rows = con.execute(
                "SELECT * FROM trades WHERE connection_id=? AND (gross_pnl IS NULL OR kalshi_close_price IS NULL) ORDER BY created_at ASC LIMIT ?",
                (connection_id, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def settle_trade(
        self,
        connection_id: str,
        trade_id: int,
        *,
        status: str,
        pnl: float,
        kalshi_close_price: float | None = None,
    ) -> None:
        with self.conn() as con:
            con.execute(
                """
                UPDATE trades
                SET status=?, gross_pnl=?,
                    kalshi_close_price=COALESCE(?, kalshi_close_price)
                WHERE id=? AND connection_id=?
                """,
                (
                    status,
                    float(pnl),
                    kalshi_close_price,
                    int(trade_id),
                    connection_id,
                ),
            )

    def daily_exposure(self, connection_id: str) -> float:
        now = time.time()
        day_start = now - (now % 86400)
        with self.conn() as con:
            row = con.execute(
                "SELECT COALESCE(SUM(requested_dollars),0) AS total FROM trades WHERE connection_id=? AND created_at>=?",
                (connection_id, day_start),
            ).fetchone()
        return float(row["total"] or 0.0)

    def daily_realized_pnl(self, connection_id: str) -> float:
        now = time.time()
        day_start = now - (now % 86400)
        with self.conn() as con:
            row = con.execute(
                "SELECT COALESCE(SUM(gross_pnl),0) AS total FROM trades WHERE connection_id=? AND created_at>=? AND gross_pnl IS NOT NULL",
                (connection_id, day_start),
            ).fetchone()
        return float(row["total"] or 0.0)

    def disconnect(self, connection_id: str) -> None:
        with self.conn() as con:
            con.execute("DELETE FROM confirmations WHERE connection_id=?", (connection_id,))
            con.execute("DELETE FROM trades WHERE connection_id=?", (connection_id,))
            con.execute("DELETE FROM settings WHERE connection_id=?", (connection_id,))
            con.execute("DELETE FROM connections WHERE id=?", (connection_id,))
