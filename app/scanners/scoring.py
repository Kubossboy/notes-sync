"""Opportunity scoring 0-100 (spec section 5) and cost-aware reward/risk.

Component scores are 0-100 or None (= module not available yet). Missing
components contribute 0 to the total and mark the signal INCOMPLETE - an
unvalidated or partial score is never presented as complete.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.data.models import AssetClass
from app.intelligence.engine import news_component
from app.sentiment.aggregate import sentiment_component
from app.strategies.base import Setup, StrategyContext, val

PENDING = {"news": "news engine unavailable or stale (no healthy ingest within news.max_health_age_minutes)",
           "sentiment": "unavailable while the news engine is not healthy",
           "quant": "strategy not validated by backtest (see `python -m app.main validation`)"}


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
    # D99: ATR exits for the leveraged main ledger (unlevered runs keep the strategy stops and min R:R 2)
    atr_exits = settings.risk.exit_mode == "atr" and atr > 0 and settings.execution.paper_leverage > 1
    if not atr_exits and not (s > 0 and risk > 0):
        problems.append("invalid stop (must be below entry)")
        risk = max(risk, 1e-12)
    if atr_exits:
        pass  # D99: the strategy stop is replaced by an ATR stop below, so its width does not matter
    elif atr and risk < 0.25 * atr:
        problems.append(f"stop too tight ({risk / atr:.2f} ATR < 0.25 ATR, inside noise)")
    max_stop_atr = float(getattr(settings.scanner, "max_stop_atr", 3.0) or 3.0)
    if not atr_exits and atr and risk > max_stop_atr * atr:
        problems.append(f"stop too wide ({risk / atr:.2f} ATR > {max_stop_atr:g} ATR)")
    target = e + setup.target_r * risk
    capped = None
    rk = settings.risk
    if atr_exits:
        # D99: ATR exits (backtest: 15m crypto 1.5 / 2.0 ATR beat the strategy stops + 100x clamp, IS and OOS)
        s = e - rk.atr_stop_mult * atr
        risk = e - s
        target = e + rk.atr_target_mult * atr
    momentum = is_momentum(ctx, settings)
    rv = val(ctx.last, "rvol20")
    min_rv = settings.scanner.stock_min_rvol if ctx.asset_class == AssetClass.STOCK else settings.scanner.crypto_min_rvol
    if min_rv and (rv is None or rv < min_rv):
        problems.append(f"rvol {0.0 if rv is None else rv:.2f} too low (needs {min_rv:g})")
    # a tested resistance between entry and target caps the realistic target (optional; D96 scalp may disable);
    # D99: not when the bar is a momentum breakout (price is already through recent highs on strong volume)
    if getattr(settings.scanner, "cap_target_by_resistance", True) and not momentum:
        obstacles = sorted(L["price"] for L in ctx.levels
                           if L["touches"] >= 2 and "high" in L["kinds"] and e + 0.25 * risk < L["price"] < target)
        if obstacles:
            capped = obstacles[0]
            target = capped
    cost = e * 2 * ctx.cost_bps_per_side / 1e4          # entry + exit, fees + slippage
    reward = target - e
    rr_g = reward / risk
    rr_n = (reward - cost) / (risk + cost)
    if settings.execution.paper_leverage > 1:
        min_rr = settings.risk.lev_min_reward_risk          # D94: 1.2 for every main-ledger strategy when leveraged
    elif setup.strategy in settings.validation.forward_test_strategies:
        min_rr = settings.validation.forward_test_min_reward_risk
    else:
        min_rr = settings.risk.min_reward_risk
    if rr_n < min_rr:
        problems.append(f"net reward/risk {rr_n:.2f} < {min_rr}"
                        + (f" (target capped by resistance {capped:.4f})" if capped else ""))
    return Plan(e, s, target, risk, reward, cost, round(rr_g, 3), round(rr_n, 3), capped, problems)


def is_momentum(ctx: StrategyContext, settings: Settings) -> bool:
    """D99: last close above the prior N-bar high AND rvol20 >= momentum_rvol."""
    n = settings.scanner.momentum_lookback
    df = ctx.df
    if len(df) < n + 1:
        return False
    prior = float(df["high"].iloc[-n - 1:-1].max())
    rv = val(ctx.last, "rvol20")
    return bool(float(df["close"].iloc[-1]) > prior and rv is not None and rv >= settings.scanner.momentum_rvol)


def cost_bps_per_side(settings: Settings, ac: AssetClass, spread_bps: float | None = None) -> float:
    ex = settings.execution
    base = (ex.crypto_fee_bps + ex.slippage_bps_crypto) if ac == AssetClass.CRYPTO else (
        ex.stock_fee_bps + ex.slippage_bps_stock)
    return base + (spread_bps / 2 if spread_bps else 0.0)


def component_scores(ctx: StrategyContext, liquidity: dict, news_cfg=None) -> dict[str, float | None]:
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
            "news": news_component(ctx.news, news_cfg) if news_cfg is not None else None,
            "sentiment": sentiment_component(ctx.sentiment), "quant": None}


def news_problems(ctx: StrategyContext, setup: Setup) -> list[str]:
    """Fundamental vetoes: a long against a verified high-impact negative catalyst is rejected."""
    n = ctx.news
    if setup.direction == "long" and n and n["health"]["healthy"] and n["negative_high_verified"]:
        e = n["negative_high_verified"][0]
        return [f"verified high-impact negative catalyst ({', '.join(e['catalysts']) or e['impact']}): "
                f"{e['title'][:80]} [{e['source']}]"]
    return []


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
