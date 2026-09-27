import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

os.environ.setdefault("FREEHOLIDAY_FERNET_KEY", Fernet.generate_key().decode())

from app import Forecast, entry_engine_analysis
from kalshi_client import Credentials, Signer
from risk import check_trade, validate_settings
from store import DEFAULT_SETTINGS, VaultStore


def test_rsa_signer_headers():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    signer = Signer(Credentials("test-key", pem))
    headers = signer.headers("GET", "/trade-api/v2/portfolio/balance")
    assert headers["KALSHI-ACCESS-KEY"] == "test-key"
    assert headers["KALSHI-ACCESS-SIGNATURE"]
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()


def test_risk_rules():
    settings = validate_settings(dict(DEFAULT_SETTINGS))
    ok = check_trade(
        settings=settings,
        asset="BTC",
        amount_dollars=2,
        contract_price=0.55,
        daily_exposure=0,
        realized_pnl=0,
        open_positions=0,
        paused=False,
    )
    assert ok.ok
    high_price = check_trade(
        settings=settings,
        asset="BTC",
        amount_dollars=2,
        contract_price=0.98,
        daily_exposure=0,
        realized_pnl=0,
        open_positions=0,
        paused=False,
    )
    assert high_price.ok


def test_ten_cent_minimum():
    settings = dict(DEFAULT_SETTINGS)
    settings["trade_size_dollars"] = 0.10
    settings["max_trade_dollars"] = 0.10
    settings["max_daily_exposure_dollars"] = 1.00
    settings["max_daily_loss_dollars"] = 1.00
    settings = validate_settings(settings)

    allowed = check_trade(
        settings=settings,
        asset="BTC",
        amount_dollars=0.10,
        contract_price=0.55,
        daily_exposure=0,
        realized_pnl=0,
        open_positions=0,
        paused=False,
    )
    assert allowed.ok

    blocked = check_trade(
        settings=settings,
        asset="BTC",
        amount_dollars=0.09,
        contract_price=0.55,
        daily_exposure=0,
        realized_pnl=0,
        open_positions=0,
        paused=False,
    )
    assert not blocked.ok


def test_multiple_trades_same_window(tmp_path):
    store = VaultStore(
        str(tmp_path / "freeholiday.db"),
        Fernet.generate_key().decode(),
    )
    connection_id, _ = store.create_connection(
        "test-key",
        "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
    )
    base = {
        "asset": "BTC",
        "window_start": 1234567890,
        "market_ticker": "TEST-BTC",
        "outcome": "YES",
        "requested_dollars": 0.10,
        "contract_price": 0.50,
        "contracts": 0.20,
        "order_id": None,
        "status": "submitted",
        "raw": {},
    }
    first = dict(base, client_order_id="order-one")
    second = dict(base, client_order_id="order-two")
    store.add_trade(connection_id, first)
    store.add_trade(connection_id, second)
    rows = store.list_trades(connection_id)
    assert len(rows) == 2
    assert rows[0]["window_start"] == rows[1]["window_start"]


def test_default_supports_four_open_markets():
    settings = validate_settings(dict(DEFAULT_SETTINGS))
    assert settings["max_open_positions"] >= 4

    allowed = check_trade(
        settings=settings,
        asset="NEAR",
        amount_dollars=0.10,
        contract_price=0.55,
        daily_exposure=0,
        realized_pnl=0,
        open_positions=3,
        paused=False,
    )
    assert allowed.ok


def test_trade_history_includes_kalshi_close(tmp_path):
    store = VaultStore(
        str(tmp_path / "close.db"),
        Fernet.generate_key().decode(),
    )
    connection_id, _ = store.create_connection(
        "test-key",
        "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
    )
    store.save_confirmation(
        connection_id,
        asset="ETH",
        window_start=2222222222,
        market_ticker="KXETH15M-TEST",
        decision="confirm",
        payload={
            "predicted_price": 4200.50,
            "threshold": 4198.00,
            "suggested_outcome": "YES",
        },
    )
    store.add_trade(
        connection_id,
        {
            "asset": "ETH",
            "window_start": 2222222222,
            "market_ticker": "KXETH15M-TEST",
            "outcome": "YES",
            "requested_dollars": 0.10,
            "contract_price": 0.60,
            "contracts": 0.16,
            "client_order_id": "close-test",
            "status": "submitted",
            "raw": {},
        },
    )
    trade_id = store.list_trades(connection_id)[0]["id"]
    store.settle_trade(
        connection_id,
        trade_id,
        status="WIN",
        pnl=0.05,
        kalshi_close_price=4201.25,
    )
    trade = store.list_trades(connection_id)[0]
    assert trade["kalshi_close_price"] == 4201.25
    assert trade["predicted_price"] == 4200.50
    assert trade["threshold"] == 4198.00


class _EntryQuote:
    def __init__(self, threshold, close_ts, yes=0.55, no=0.45):
        self.threshold = threshold
        self.close_ts = close_ts
        self._yes = yes
        self._no = no

    def outcome_price(self, outcome):
        return self._yes if outcome == "YES" else self._no


def test_entry_v2_does_not_double_count_move():
    forecast = Forecast(
        asset="BTC",
        direction="Higher",
        predicted_price=102.0,
        confidence=0.65,
        window_start=1_000_000,
        window_end=1_900_000,
        locked_price=100.0,
        forecast_error_sigma_pct=0.01,
        forecast_error_samples=10,
    )
    quote = _EntryQuote(threshold=101.0, close_ts=1900.0)

    analysis = entry_engine_analysis(
        forecast,
        quote,
        current_price=102.0,
        calibration={"samples": 10, "sigma": 0.01, "bias": 0.0},
        now=1450.0,
    )

    assert abs(analysis["live_entry_forecast"] - 102.0) < 1e-9


def test_entry_v2_respects_late_window_live_price():
    forecast = Forecast(
        asset="BTC",
        direction="Higher",
        predicted_price=102.0,
        confidence=0.65,
        window_start=1_000_000,
        window_end=1_900_000,
        locked_price=100.0,
        forecast_error_sigma_pct=0.01,
        forecast_error_samples=10,
    )
    quote = _EntryQuote(threshold=100.0, close_ts=1900.0)

    analysis = entry_engine_analysis(
        forecast,
        quote,
        current_price=99.0,
        calibration={"samples": 10, "sigma": 0.01, "bias": 0.0},
        now=1870.0,
    )

    assert analysis["live_entry_forecast"] < 100.0
    assert analysis["suggested_outcome"] == "NO"
