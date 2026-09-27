from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from kalshi_client import Credentials, Signer
from risk import check_trade, validate_settings
from cryptography.fernet import Fernet

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
