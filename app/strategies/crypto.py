"""Crypto strategies (24/7, 15m by default). Crypto-specific filters: order-book
spread/depth, UTC-day VWAP, minimum volatility vs. round-trip costs, and
order-book based setups that have no stock equivalent here."""
from __future__ import annotations

from statistics import median

from app.strategies import stock as S
from app.strategies.base import NoSetup, Setup, StrategyContext, need, req, val


def _vol_vs_costs(ctx: StrategyContext) -> None:
    (atr_pct,) = req(ctx.last, "atr_pct")
    rt_cost_pct = 2 * ctx.cost_bps_per_side / 100
    need(atr_pct >= rt_cost_pct, f"ATR {atr_pct:.3f}% below round-trip cost {rt_cost_pct:.3f}%")


def momentum_breakout(ctx, p):
    _vol_vs_costs(ctx)
    return S.momentum_breakout(ctx, p, name="momentum_breakout")


def volume_expansion(ctx: StrategyContext, p: dict) -> Setup:
    _vol_vs_costs(ctx)
    d, last = ctx.df, ctx.last
    k = int(p.get("bars", 2))
    win = d.iloc[-k:]
    need(win["rvol20"].notna().all(), "insufficient history for rvol20")
    need(bool((win["rvol20"] >= p.get("min_rvol", 2.5)).all()), f"volume not expanded on last {k} bars")
    need(bool(win["close"].is_monotonic_increasing) and last["close"] > last["open"], "price not rising")
    e20, atr = req(last, "ema20", "atr14")
    need(last["close"] > e20, "below EMA20")
    entry = float(last["close"])
    return Setup("volume_expansion", ctx.symbol, "long", entry, float(win["low"].min()) - 0.1 * atr,
                 p.get("target_r", 2.5), {"rvol": [round(float(x), 2) for x in win["rvol20"]]})


def orderbook_imbalance(ctx: StrategyContext, p: dict) -> Setup:
    _vol_vs_costs(ctx)
    need(ctx.book is not None, "no order book snapshot")
    pct = p.get("depth_pct", 0.5)
    imb = ctx.book.imbalance(pct)
    need(imb >= p.get("min_imbalance", 0.3), f"bid/ask imbalance {imb:.2f} below threshold")
    last = ctx.last
    e20, atr = req(last, "ema20", "atr14")
    vw = val(last, "vwap")
    need(last["close"] > e20 and (vw is None or last["close"] > vw), "price below EMA20/VWAP")
    need(last["close"] > last["open"], "last bar not bullish")
    b, a = ctx.book.depth_usd(pct)
    entry = float(last["close"])
    return Setup("orderbook_imbalance", ctx.symbol, "long", entry, entry - 1.5 * atr, p.get("target_r", 2.5),
                 {"imbalance": round(imb, 3), "bid_depth_usd": round(b), "ask_depth_usd": round(a),
                  "book_source": ctx.book.source,
                  "caveat": "order-book imbalance can be spoofed; single snapshot"})


def volatility_expansion(ctx: StrategyContext, p: dict) -> Setup:
    _vol_vs_costs(ctx)
    d, last, prev = ctx.df, ctx.last, ctx.prev
    lb = int(p.get("squeeze_lookback", 100))
    w = d["bb_width"].iloc[-lb - 1:-1].dropna()
    need(len(w) >= lb * 0.8, "insufficient history for squeeze")
    thr = float(w.quantile(p.get("squeeze_pctile", 20) / 100))
    pw = val(prev, "bb_width")
    need(pw is not None and pw <= thr, "no Bollinger squeeze on previous bar")
    up, mid, atr = req(last, "bb_upper", "bb_mid", "atr14")
    need(last["close"] > up, "close not above upper band")
    a5 = val(d.iloc[-6], "atr14")
    need(a5 is not None and atr > a5, "ATR not expanding")
    entry = float(last["close"])
    return Setup("volatility_expansion", ctx.symbol, "long", entry, mid, p.get("target_r", 2.5),
                 {"bb_width_prev": pw, "squeeze_threshold": thr})


def trend_continuation(ctx, p):
    _vol_vs_costs(ctx)
    return S.trend_continuation(ctx, p, require_sma200=False)


def sr_breakout(ctx, p):
    _vol_vs_costs(ctx)
    return S.sr_breakout(ctx, p)


def liquidity_change(ctx: StrategyContext, p: dict) -> Setup:
    _vol_vs_costs(ctx)
    need(ctx.book is not None, "no order book snapshot")
    hist = [h for h in ctx.book_history if h.get("ask_depth_1_usd") and h.get("bid_depth_1_usd")]
    need(len(hist) >= p.get("min_history", 5), f"only {len(hist)} stored book snapshots (need history)")
    mb, ma = median(h["bid_depth_1_usd"] for h in hist), median(h["ask_depth_1_usd"] for h in hist)
    b, a = ctx.book.depth_usd(1.0)
    ask_drop = (1 - a / ma) * 100 if ma else 0
    bid_drop = (1 - b / mb) * 100 if mb else 0
    need(ask_drop >= p.get("ask_drop_pct", 50), f"ask depth change {-ask_drop:.0f}% (no thinning)")
    need(bid_drop <= p.get("max_bid_drop_pct", 20), f"bid depth also fell {bid_drop:.0f}% (liquidity exit, not a long)")
    e20, atr = req(ctx.last, "ema20", "atr14")
    need(ctx.last["close"] > e20, "below EMA20")
    entry = float(ctx.last["close"])
    return Setup("liquidity_change", ctx.symbol, "long", entry, entry - 1.5 * atr, p.get("target_r", 2.5),
                 {"ask_depth_drop_pct": round(ask_drop, 1), "bid_depth_drop_pct": round(bid_drop, 1),
                  "history_snapshots": len(hist)})


def liquidity_alert(ctx: StrategyContext) -> str | None:
    """Risk flag (not a signal): both sides of the book thinned sharply."""
    hist = [h for h in ctx.book_history if h.get("ask_depth_1_usd") and h.get("bid_depth_1_usd")]
    if ctx.book is None or len(hist) < 5:
        return None
    mb, ma = median(h["bid_depth_1_usd"] for h in hist), median(h["ask_depth_1_usd"] for h in hist)
    b, a = ctx.book.depth_usd(1.0)
    if mb and ma and b < 0.5 * mb and a < 0.5 * ma:
        return f"liquidity drop: bid depth {b / mb:.0%}, ask depth {a / ma:.0%} of recent median"
    return None


CRYPTO_STRATEGIES = {
    "momentum_breakout": momentum_breakout,
    "volume_expansion": volume_expansion,
    "orderbook_imbalance": orderbook_imbalance,
    "volatility_expansion": volatility_expansion,
    "trend_continuation": trend_continuation,
    "sr_breakout": sr_breakout,
    "liquidity_change": liquidity_change,
}
