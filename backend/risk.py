from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class RiskDecision:
    ok: bool
    reason: str = ""


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    out = dict(settings)
    numeric = {
        "trade_size_dollars": (0.10, 10000.0),
        "max_trade_dollars": (0.10, 10000.0),
        "max_daily_exposure_dollars": (0.10, 100000.0),
        "max_daily_loss_dollars": (0.10, 100000.0),
    }
    for key, (lo, hi) in numeric.items():
        value = float(out[key])
        if not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        out[key] = value
    open_positions = int(out["max_open_positions"])
    if not 1 <= open_positions <= 100:
        raise ValueError("max_open_positions must be between 1 and 100")
    out["max_open_positions"] = open_positions
    out["auto_trade"] = bool(out.get("auto_trade"))
    allowed = [str(x).upper() for x in out.get("allowed_assets", [])]
    out["allowed_assets"] = [x for x in allowed if x in ("BTC", "ETH", "DOGE", "NEAR")]
    if out["trade_size_dollars"] > out["max_trade_dollars"]:
        raise ValueError("trade_size_dollars cannot exceed max_trade_dollars")
    if out["max_trade_dollars"] > out["max_daily_exposure_dollars"]:
        raise ValueError("max_trade_dollars cannot exceed max_daily_exposure_dollars")
    return out


def check_trade(
    *,
    settings: dict[str, Any],
    asset: str,
    amount_dollars: float,
    contract_price: float,
    daily_exposure: float,
    realized_pnl: float,
    open_positions: int,
    paused: bool,
) -> RiskDecision:
    if paused:
        return RiskDecision(False, "Trading is paused")
    if asset.upper() not in settings.get("allowed_assets", []):
        return RiskDecision(False, f"{asset.upper()} is disabled in trading settings")
    if amount_dollars < 0.10 - 1e-9:
        return RiskDecision(False, "Minimum trade size is $0.10")
    if amount_dollars > float(settings["max_trade_dollars"]) + 1e-9:
        return RiskDecision(False, "Trade exceeds the per-trade dollar limit")
    if daily_exposure + amount_dollars > float(settings["max_daily_exposure_dollars"]) + 1e-9:
        return RiskDecision(False, "Trade would exceed the daily exposure limit")
    if realized_pnl <= -float(settings["max_daily_loss_dollars"]):
        return RiskDecision(False, "Daily loss limit has been reached")
    if open_positions >= int(settings["max_open_positions"]):
        return RiskDecision(False, "Maximum open positions reached")
    return RiskDecision(True)
