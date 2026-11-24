"""US stock intraday strategies (regular session bars, 5m by default)."""
from __future__ import annotations

from app.data.calendar import ET
from app.strategies.base import NoSetup, Setup, StrategyContext, need, req, val


def _rvol(row) -> float | None:
    # time-of-day RVOL is the right measure for stocks (the open is always busy)
    v = val(row, "rvol_tod")
    return v if v is not None else val(row, "rvol20")


def _today(ctx: StrategyContext):
    d = ctx.df
    dates = d.index.tz_convert(ET).date
    return d[dates == dates[-1]]


def opening_range_breakout(ctx: StrategyContext, p: dict) -> Setup:
    today = _today(ctx)
    step_min = int(ctx.df.index.to_series().diff().dropna().dt.total_seconds().median() / 60) if len(ctx.df) > 2 else 5
    n_or = max(1, int(p.get("or_minutes", 15) // max(step_min, 1)))
    need(len(today) > n_or, f"opening range not complete ({len(today)}/{n_or + 1} bars today)")
    first_et = today.index[0].tz_convert(ET)
    need(first_et.hour == 9 and first_et.minute == 30, "today's session does not start at 09:30 ET in data")
    orng = today.iloc[:n_or]
    or_high, or_low = float(orng["high"].max()), float(orng["low"].min())
    last, prev = today.iloc[-1], today.iloc[-2]
    mins = (today.index[-1] - today.index[0]).total_seconds() / 60
    need(mins <= p.get("max_minutes_after_open", 120), f"{mins:.0f} min after open > window")
    need(last["close"] > or_high, "close not above opening-range high")
    need(prev["close"] <= or_high, "breakout not fresh (previous bar already above)")
    rv = _rvol(last)
    need(rv is not None and rv >= p.get("min_rvol", 1.5), f"rvol {rv} below {p.get('min_rvol', 1.5)}")
    (atr,) = req(last, "atr14")
    entry = float(last["close"])
    stop = max(or_low, entry - 2 * atr)
    return Setup("opening_range_breakout", ctx.symbol, "long", entry, stop, p.get("target_r", 2.5), {
        "or_high": or_high, "or_low": or_low, "or_bars": n_or, "minutes_after_open": mins, "rvol": rv,
        "premarket": premarket_context(ctx)})


def vwap_reclaim(ctx: StrategyContext, p: dict) -> Setup:
    d = ctx.df
    need(len(d) >= 10, "not enough bars")
    last, prev = ctx.last, ctx.prev
    vw, pvw, atr = req(last, "vwap") + req(prev, "vwap") + req(last, "atr14")
    lb = int(p.get("lookback", 6))
    win = d.iloc[-lb - 1:-1]
    below = int((win["close"] < win["vwap"]).sum())
    need(below >= p.get("min_bars_below", 3), f"only {below} of last {lb} bars below VWAP")
    need(prev["close"] < pvw and last["close"] > vw, "no VWAP reclaim on the last bar")
    e9, pe9 = req(last, "ema9") + req(prev, "ema9")
    need(e9 > pe9, "EMA9 not rising")
    rv = _rvol(last)
    need(rv is not None and rv >= p.get("min_rvol", 1.2), f"rvol {rv} too low")
    entry = float(last["close"])
    stop = float(d["low"].iloc[-3:].min()) - 0.1 * atr
    return Setup("vwap_reclaim", ctx.symbol, "long", entry, stop, p.get("target_r", 2.5),
                 {"vwap": vw, "bars_below": below, "rvol": rv})


def momentum_breakout(ctx: StrategyContext, p: dict, name: str = "momentum_breakout") -> Setup:
    d, last = ctx.df, ctx.last
    n = int(p.get("donchian", 20))
    need(len(d) > n + 1, "not enough bars")
    e9, e20, e50, rsi, roc, atr = req(last, "ema9", "ema20", "ema50", "rsi14", "roc10", "atr14")
    prior_high = float(d["high"].iloc[-n - 1:-1].max())
    need(last["close"] > prior_high, f"close not above {n}-bar high")
    need(e9 > e20 > e50, "EMA 9/20/50 not stacked bullish")
    need(roc >= p.get("min_roc", 0.3), f"ROC10 {roc:.2f}% too low")
    need(rsi <= p.get("rsi_max", 80), f"RSI {rsi:.0f} overextended")
    rv = _rvol(last) if ctx.asset_class.value == "stock" else val(last, "rvol20")
    need(rv is not None and rv >= p.get("min_rvol", 1.5), f"rvol {rv} too low")
    entry = float(last["close"])
    stop = entry - 1.5 * atr
    return Setup(name, ctx.symbol, "long", entry, stop, p.get("target_r", 2.5),
                 {"breakout_level": prior_high, "donchian": n, "roc10": roc, "rsi": rsi, "rvol": rv})


def relative_volume_anomaly(ctx: StrategyContext, p: dict) -> Setup:
    last = ctx.last
    rv = _rvol(last)
    need(rv is not None and rv >= p.get("min_rvol", 3.0), f"rvol {rv} below anomaly threshold")
    (atr,) = req(last, "atr14")
    rng = last["high"] - last["low"]
    need(rng > 0 and last["close"] > last["open"], "bar not bullish")
    pos = (last["close"] - last["low"]) / rng
    need(pos >= p.get("close_in_range", 0.75), f"close at {pos:.0%} of bar range")
    vw = val(last, "vwap")
    need(vw is None or last["close"] > vw, "below VWAP")
    entry = float(last["close"])
    return Setup("relative_volume_anomaly", ctx.symbol, "long", entry, float(last["low"]) - 0.1 * atr,
                 p.get("target_r", 2.5), {"rvol": rv, "close_in_range": pos})


def trend_continuation(ctx: StrategyContext, p: dict, require_sma200: bool = True,
                       name: str = "trend_continuation") -> Setup:
    d, last = ctx.df, ctx.last
    e9, e20, rsi, atr = req(last, "ema9", "ema20", "rsi14", "atr14")
    need(last.get("trend") == "up", f"trend is {last.get('trend')}")
    if require_sma200:
        (s200,) = req(last, "sma200")
        need(last["close"] > s200, "below SMA200")
    k = int(p.get("pullback_bars", 3))
    win = d.iloc[-k - 1:-1]
    need(bool((win["low"] <= win["ema20"] * 1.001).any()), "no pullback to EMA20")
    need(last["close"] > e9 and last["close"] > last["open"], "no bullish resumption bar above EMA9")
    need(p.get("rsi_min", 40) <= rsi <= p.get("rsi_max", 65), f"RSI {rsi:.0f} outside range")
    entry = float(last["close"])
    stop = float(d["low"].iloc[-k - 1:].min()) - 0.2 * atr
    return Setup(name, ctx.symbol, "long", entry, stop, p.get("target_r", 2.5),
                 {"pullback_low": float(d["low"].iloc[-k - 1:].min()), "rsi": rsi})


def sr_breakout(ctx: StrategyContext, p: dict, name: str = "sr_breakout") -> Setup:
    last, prev = ctx.last, ctx.prev
    (atr,) = req(last, "atr14")
    lv = [L for L in ctx.levels if L["touches"] >= p.get("min_touches", 2) and "high" in L["kinds"]
          and prev["close"] < L["price"] <= last["close"]]
    need(bool(lv), "no tested resistance broken on the last bar")
    L = max(lv, key=lambda x: x["touches"])
    rv = _rvol(last) if ctx.asset_class.value == "stock" else val(last, "rvol20")
    need(rv is not None and rv >= p.get("min_rvol", 1.3), f"rvol {rv} too low")
    entry = float(last["close"])
    return Setup(name, ctx.symbol, "long", entry, L["price"] - 0.5 * atr, p.get("target_r", 2.5),
                 {"level": L["price"], "touches": L["touches"], "rvol": rv})


def news_momentum(ctx: StrategyContext, p: dict) -> Setup:
    """Fresh positive catalyst (>= medium impact, confident) + the market confirming it:
    close above VWAP, bullish bar, new N-bar high, strong time-of-day RVOL."""
    n = ctx.news
    need(n is not None and n["health"]["healthy"], "news engine unavailable or stale")
    need(not n["negative_high_verified"], "verified high-impact negative catalyst present")
    cats = [c for c in n["positive_catalysts"] if (c["confidence"] or 0) >= p.get("min_confidence", 0.6)
            and (c.get("verified") or not p.get("require_verified", 1))]
    need(bool(cats), "no fresh VERIFIED positive catalyst (>= medium impact, confidence >= "
                     f"{p.get('min_confidence', 0.6)})")
    d = ctx.df
    lb = int(p.get("high_lookback", 6))
    need(len(d) > lb + 1, "not enough bars")
    last = ctx.last
    vw, atr = req(last, "vwap") + req(last, "atr14")
    need(last["close"] > vw, "close below VWAP (market not confirming the news)")
    need(last["close"] > last["open"], "last bar not bullish")
    prior_high = float(d["high"].iloc[-lb - 1:-1].max())
    need(last["close"] > prior_high, f"no new {lb}-bar high")
    rv = _rvol(last)
    need(rv is not None and rv >= p.get("min_rvol", 2.0), f"rvol {rv} below {p.get('min_rvol', 2.0)}")
    entry = float(last["close"])
    stop = float(d["low"].iloc[-3:].min()) - 0.1 * atr
    return Setup("news_momentum", ctx.symbol, "long", entry, stop, p.get("target_r", 2.5), {
        "catalysts": cats[:3], "news_aggregate": n["aggregate"], "rvol": rv, "prior_high": prior_high})


def premarket_context(ctx: StrategyContext) -> dict | None:
    a = ctx.df_all
    if a is None or a.empty or "session" not in a:
        return None
    dates = a.index.tz_convert(ET).date
    today = a[dates == ctx.df.index[-1].tz_convert(ET).date()]
    pre = today[today["session"] == "pre"]
    reg = ctx.df
    prev_days = reg[reg.index.tz_convert(ET).date < ctx.df.index[-1].tz_convert(ET).date()]
    if pre.empty or prev_days.empty:
        return None
    pc = float(prev_days["close"].iloc[-1])
    return {"premarket_high": float(pre["high"].max()), "premarket_low": float(pre["low"].min()),
            "gap_pct": round((float(pre["close"].iloc[-1]) / pc - 1) * 100, 3), "prev_close": pc}


STOCK_STRATEGIES = {
    "opening_range_breakout": opening_range_breakout,
    "vwap_reclaim": vwap_reclaim,
    "momentum_breakout": momentum_breakout,
    "relative_volume_anomaly": relative_volume_anomaly,
    "trend_continuation": trend_continuation,
    "sr_breakout": sr_breakout,
    "news_momentum": news_momentum,
}
