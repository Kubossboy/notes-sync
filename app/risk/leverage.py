"""SIMULATED isolated leverage for the MAIN paper ledger (DECISIONS D94, explicit user request; fictional money).
Long only. Margin = notional / L; liquidation (Bybit-style isolated, maintenance margin mmr):
liq = entry x (1 - 1/L + mmr); on liquidation the loss is the whole margin (exit at entry x (1 - 1/L)) plus fees.
The shadow ledger and the backtester stay unlevered (L = 1)."""
from __future__ import annotations


def lev(s) -> float:
    return float(s.execution.paper_leverage)


def liq_price(entry: float, L: float, mmr_pct: float) -> float | None:
    return entry * (1 - 1 / L + mmr_pct / 100) if L > 1 else None


def bankrupt_price(entry: float, L: float) -> float:
    return entry * (1 - 1 / L)


def risk_pct(s) -> float:
    L = lev(s)
    return min(s.risk.max_risk_per_trade_pct * L, s.risk.lev_max_risk_per_trade_pct) if L > 1 else s.risk.max_risk_per_trade_pct


def daily_pct(s) -> float:
    L = lev(s)
    return min(s.risk.max_daily_loss_pct * L, s.risk.lev_max_daily_loss_pct) if L > 1 else s.risk.max_daily_loss_pct


def scalp_levels(s, fill: float, stop: float, target: float, atr15: float | None) -> tuple[float, float]:
    """Tighter levels for leveraged trades (never wider than the signal's): TP = min(signal target, fill + k x ATR15);
    SL distance = min(signal SL distance, TP distance / 1.5, 60 % of the distance to liquidation)."""
    L = lev(s)
    tp_d = target - fill
    if atr15 and atr15 > 0:
        tp_d = min(tp_d, s.risk.lev_tp_atr * atr15)
    liq = liq_price(fill, L, s.execution.maintenance_margin_pct)
    sl_d = min(fill - stop, tp_d / 1.5, s.risk.lev_sl_liq_fraction * (fill - liq))
    return fill - sl_d, fill + tp_d
