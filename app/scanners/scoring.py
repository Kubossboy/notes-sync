"""Opportunity scoring 0-100 (spec section 5) and cost-aware reward/risk.

Component scores are 0-100 or None (= module not available yet). Missing
components contribute 0 to the total and mark the signal INCOMPLETE - an
unvalidated or partial score is never presented as complete.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.data.models import AssetClass
from app.strategies.base import Setup, StrategyContext, val

PENDING = {"news": "news engine (milestone 4)", "sentiment": "sentiment engine (milestone 4)",
           "quant": "statistical validation / backtests (milestone 6)"}


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


@dataclass
class Plan:
    entry: float
    stop: float
    target: float
    risk_per_unit: float
    reward_per_unit: float
    cost_per_unit: float
    reward_risk_gross: float
    reward_risk_net: float
    target_capped_by: float | None = None
    problems: list[str] = field(default_factory=list)


def build_plan(setup: Setup, ctx: StrategyContext, settings: Settings) -> Plan:
    e, s = setup.entry, setup.stop
    problems = []
    atr = val(ctx.last, "atr14") or 0.0
    risk = e - s
    if not (s > 0 and risk > 0):
        problems.append("invalid stop (must be below entry)")
        risk = max(risk, 1e-12)
    if atr and risk < 0.25 * atr:
        problems.append(f"stop too tight ({risk / atr:.2f} ATR < 0.25 ATR, inside noise)")
    if atr and risk > 3 * atr:
        problems.append(f"stop too wide ({risk / atr:.2f} ATR > 3 ATR)")
    target = e + setup.target_r * risk
    capped = None
    # a tested resistance between entry and target caps the realistic target
    obstacles = sorted(L["price"] for L in ctx.levels
                       if L["touches"] >= 2 and "high" in L["kinds"] and e + 0.25 * risk < L["price"] < target)
    if obstacles:
        capped = obstacles[0]
        target = capped
    cost = e * 2 * ctx.cost_bps_per_side / 1e4          # entry + exit, fees + slippage
    reward = target - e
    rr_g = reward / risk
    rr_n = (reward - cost) / (risk + cost)
    if rr_n < settings.risk.min_reward_risk:
        problems.append(f"net reward/risk {rr_n:.2f} < {settings.risk.min_reward_risk}"
                        + (f" (target capped by resistance {capped:.4f})" if capped else ""))
    return Plan(e, s, target, risk, reward, cost, round(rr_g, 3), round(rr_n, 3), capped, problems)


def cost_bps_per_side(settings: Settings, ac: AssetClass, spread_bps: float | None = None) -> float:
    ex = settings.execution
    base = (ex.crypto_fee_bps + ex.slippage_bps_crypto) if ac == AssetClass.CRYPTO else (
        ex.stock_fee_bps + ex.slippage_bps_stock)
    return base + (spread_bps / 2 if spread_bps else 0.0)


def component_scores(ctx: StrategyContext, liquidity: dict) -> dict[str, float | None]:
    last, prev = ctx.last, ctx.prev
    c = float(last["close"])
    checks = []
    for a, b in (("ema9", "ema20"), ("ema20", "ema50")):
        va, vb = val(last, a), val(last, b)
        if va is not None and vb is not None:
            checks.append(va > vb)
    for col in ("vwap", "sma200"):
        v = val(last, col)
        if v is not None:
            checks.append(c > v)
    r = val(last, "rsi14")
    if r is not None:
        checks.append(50 <= r <= 75)
    h = val(last, "macd_hist")
    if h is not None:
        checks.append(h > 0)
    technical = _clamp(100 * sum(checks) / len(checks)) if checks else None

    mv = []
    rv = val(last, "rvol_tod") if ctx.asset_class == AssetClass.STOCK else None
    rv = rv if rv is not None else val(last, "rvol20")
    if rv is not None:
        mv.append(_clamp((rv - 1) / 2 * 100))            # rvol 1 -> 0, 3 -> 100
    roc, ap = val(last, "roc10"), val(last, "atr_pct")
    if roc is not None and ap:
        mv.append(_clamp(roc / (2 * ap) * 100))          # 10-bar move of 2 ATR -> 100
    ph = val(prev, "macd_hist")
    if h is not None and ph is not None:
        mv.append(100.0 if h > ph else 0.0)
    momentum = sum(mv) / len(mv) if mv else None

    return {"technical": technical, "momentum_volume": momentum, "liquidity": liquidity.get("score"),
            "news": None, "sentiment": None, "quant": None}


def liquidity_assessment(ctx: StrategyContext, settings: Settings, spread_bps: float | None) -> dict:
    sc = settings.scanner
    out: dict = {"spread_bps": spread_bps, "problems": []}
    if ctx.asset_class == AssetClass.STOCK:
        d = ctx.df.tail(20)
        dv = float((d["close"] * d["volume"]).mean()) if len(d) else 0.0
        out["avg_dollar_volume_per_bar"] = round(dv)
        price = float(ctx.last["close"])
        if price < sc.stock_min_price:
            out["problems"].append(f"price {price} < {sc.stock_min_price}")
        if dv < sc.stock_min_avg_dollar_volume:
            out["problems"].append(f"avg $vol/bar {dv:,.0f} < {sc.stock_min_avg_dollar_volume:,.0f}")
        parts = [_clamp(dv / sc.stock_min_avg_dollar_volume * 50)] if sc.stock_min_avg_dollar_volume else [100.0]
        if spread_bps is not None:
            parts.append(_clamp(100 - (spread_bps - 5) / 45 * 100))
        out["score"] = sum(parts) / len(parts)
    else:
        if ctx.book is None:
            out["problems"].append("no order book")
            out["score"] = None
            return out
        b, a = ctx.book.depth_usd(0.5)
        sp = ctx.book.spread_bps
        out.update({"spread_bps": round(sp, 3), "bid_depth_05_usd": round(b), "ask_depth_05_usd": round(a)})
        if sp > sc.crypto_max_spread_bps:
            out["problems"].append(f"spread {sp:.1f} bps > {sc.crypto_max_spread_bps}")
        if min(b, a) < sc.crypto_min_depth_usd_05:
            out["problems"].append(f"depth within 0.5% {min(b, a):,.0f} USD < {sc.crypto_min_depth_usd_05:,.0f}")
        depth_score = _clamp(min(b, a) / (sc.crypto_min_depth_usd_05 * 10) * 100) if sc.crypto_min_depth_usd_05 else 100
        out["score"] = (_clamp(100 - sp / sc.crypto_max_spread_bps * 100) + depth_score) / 2
    return out


def total_score(components: dict[str, float | None], settings: Settings) -> tuple[float, float, float]:
    w = settings.scoring.weights.model_dump()
    total = sum(w[k] * (v or 0.0) for k, v in components.items())
    avail = sum(w[k] for k, v in components.items() if v is not None)
    on_avail = sum(w[k] * v for k, v in components.items() if v is not None) / avail if avail else 0.0
    return round(total, 2), round(avail, 3), round(on_avail, 2)
