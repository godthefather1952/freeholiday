from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from kalshi_client import Credentials, Signer
from risk import check_trade, validate_settings
from store import DEFAULT_SETTINGS


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
    blocked = check_trade(
        settings=settings,
        asset="BTC",
        amount_dollars=2,
        contract_price=0.90,
        daily_exposure=0,
        realized_pnl=0,
        open_positions=0,
        paused=False,
    )
    assert not blocked.ok
