"""Plain-English explanations of scanner / risk reason codes for the public dashboard (no jargon).

explain(reason, tf="15m", orb_window=120) -> one readable sentence (or several joined), never a raw code
when a rule matches. Unknown codes are returned unchanged so nothing is silently hidden.
"""
from __future__ import annotations

import re

N = r"(-?[\d.,e+-]+)"
TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def _num(x: str) -> float:
    return float(x.replace(",", "").rstrip("."))


def _f(x, d: int = 2) -> str:
    try:
        v = _num(x) if isinstance(x, str) else float(x)
    except ValueError:
        return str(x)
    s = f"{v:,.{d}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _usd(x: str) -> str:
    v = _num(x)
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}k"


def _span(bars: int, tf: str) -> str:
    mins = bars * TF_MIN.get(tf, 15)
    if mins < 120:
        return f"{mins} minutes"
    h = mins / 60
    if h < 48:
        return f"{_f(h, 1)} hours"
    return f"{_f(h / 24, 1)} days"


def _rr(m, ctx, capped: bool) -> str:
    v, need = _num(m[1]), _f(m[2], 1)
    if v <= 0:
        s = "Skipped: fees would eat the whole profit."
    else:
        t = int(v * 100) / 100                     # truncate so 1.196 never reads as "1.2 (needs 1.2)"
        if t >= _num(m[2]):                        # rounded text: 1.196 is logged as "1.20 < 1.2"
            s = f"Skipped: after fees, the profit is just under the {need}x-the-risk minimum (a very close call)."
        else:
            s = f"Skipped: after fees, the profit would be only {t:.2f}x the risk (needs {need}x)."
    if capped:
        s += f" Price is near a ceiling at {_f(m[3])}, so there is little room to rise."
    return s


STRATEGY = {
    "opening_range_breakout": "Opening breakout", "vwap_reclaim": "Back above day's average",
    "momentum_breakout": "Momentum breakout", "relative_volume_anomaly": "Unusual volume",
    "trend_continuation": "Trend continuation", "sr_breakout": "Ceiling breakout", "manual_news": "Manual (news)",
    "news_catalyst": "News catalyst", "bollinger_squeeze": "Quiet-then-breakout",
    "orderbook_imbalance": "Buyers outweigh sellers", "liquidity_change": "Sellers thinning out",
}
EXIT = {"take_profit": "Hit take-profit", "stop_loss": "Hit stop-loss", "time_exit": "Max holding time reached",
        "manual": "Closed manually", "liquidation": "Liquidated (leverage)", "trailing_stop": "Hit trailing stop",
        "eod": "End of trading day"}

RULES: list[tuple[str, object]] = [
    (rf"net reward/risk {N} < {N} \(target capped by resistance {N}\)", lambda m, c: _rr(m, c, True)),
    (rf"net reward/risk {N} < {N}", lambda m, c: _rr(m, c, False)),
    (rf"avg \$vol/bar {N} < {N}", lambda m, c: f"Too little trading volume ({_usd(m[1])} per candle, needs {_usd(m[2])})."),
    (rf"^price {N} < {N}", lambda m, c: f"Stock is too cheap (under ${_f(m[2], 0)})."),
    (r"close not above (\d+)-?bar high",
     lambda m, c: f"No breakout: price has not beaten its high of the last {_span(int(m[1]), c['tf'])}."),
    (r"no tested resistance broken on the last bar", lambda m, c: "No breakout: price did not push through a proven ceiling."),
    (r"no fresh VERIFIED positive catalyst.*", lambda m, c: "No fresh, confirmed good news about it."),
    (r"(\d+) min after open > window",
     lambda m, c: f"Too late for the opening-breakout strategy (only works in the first {c['orb']} min after market open)."),
    (rf"rvol {N} below anomaly threshold", lambda m, c: f"Trading volume is only {_f(m[1], 1)}x normal, not unusual enough."),
    (rf"rvol {N} too low", lambda m, c: f"Trading volume is weak ({_f(m[1], 1)}x normal)."),
    (r"only (\d+) of last (\d+) bars below VWAP",
     lambda m, c: "Price never really dipped below the day's average, so there is no rebound to buy."),
    (rf"ATR {N}% below round-trip cost {N}%",
     lambda m, c: f"Price moves too little ({_f(m[1])}% per candle) to beat the fees ({_f(m[2])}%)."),
    (r"volume not expanded.*", lambda m, c: "Trading volume is not picking up."),
    (r"no Bollinger squeeze.*", lambda m, c: "No calm period before a breakout."),
    (r"trend is down", lambda m, c: "Price is falling, the bot only buys rising markets."),
    (r"trend is range", lambda m, c: "Price is moving sideways, there is no trend to follow."),
    (r"no pullback to EMA(\d+)", lambda m, c: "Price has not dipped back for a cheaper entry yet."),
    (rf"bid/ask imbalance {N} below threshold", lambda m, c: "Buyers do not clearly outnumber sellers."),
    (r"no bullish resumption bar.*", lambda m, c: "Waiting for a confirming up-move."),
    (r"ask depth change .*\(no thinning\)", lambda m, c: "Sell orders are not drying up."),
    (r"data not tradable.*", lambda m, c: "Price data is delayed, so it was skipped."),
    (r"market closed", lambda m, c: "Market is closed."),
    (r"no VWAP reclaim.*", lambda m, c: "Price has not climbed back above the day's average."),
    (r"only (\d+) stored book snapshots.*", lambda m, c: "Still collecting order-book history."),
    (r"below SMA(\d+)", lambda m, c: "Price is below its long-term average (weak)."),
    (r"price below EMA\d+/VWAP|below VWAP", lambda m, c: "Price is below the day's average (weak)."),
    (r"close not above upper band", lambda m, c: "Price stayed inside its normal range."),
    (r"(last )?bar not bullish", lambda m, c: "Last candle went down, not up."),
    (r"close not above opening-range high", lambda m, c: "Price has not beaten the morning high."),
    (r"breakout not fresh.*", lambda m, c: "The breakout happened earlier, too late to join."),
    (r"close at .*% of bar range", lambda m, c: "Candle closed weak, far from its high."),
    (r"ROC\d* .*too low", lambda m, c: "Price is not rising fast enough."),
    (r"EMA .*not stacked bullish", lambda m, c: "Short and long averages do not line up for an uptrend."),
    (r"price not rising", lambda m, c: "Price is not rising."),
    (r"ATR not expanding", lambda m, c: "Price swings are not growing."),
    (r"quarantined: cross-source divergence.*", lambda m, c: "Exchanges disagree on the price, so it was skipped."),
    (r"quarantined: no valid market data.*", lambda m, c: "No usable price data, so it was skipped."),
    (r"stop .*wider than.*|stop too wide.*", lambda m, c: "The stop-loss would be too far away (too risky)."),
    (rf"score {N} < {N}", lambda m, c: f"Overall score {_f(m[1], 0)}/100 is below the {_f(m[2], 0)} needed."),
    (r"RSI .*outside range", lambda m, c: "Price has run too hot or too cold (RSI out of range)."),
    (r"no valid data", lambda m, c: "No usable price data."),
    (r"SAFE STATE active", lambda m, c: "Bot is paused for safety."),
    (r"disabled in config", lambda m, c: "Strategy is switched off."),
    (r"max open positions.*|open positions .*>=.*", lambda m, c: "Already holding the maximum number of trades."),
    (r"duplicate.*", lambda m, c: "Same signal was already handled."),
    (r"cooldown.*", lambda m, c: "Symbol is paused after a recent loss."),
    (r"spread .*", lambda m, c: "Gap between buy and sell price is too wide."),
]
_COMPILED = [(re.compile(p, re.I), f) for p, f in RULES]


def explain_one(part: str, tf: str = "15m", orb_window: int = 120) -> str:
    part = part.strip()
    if ": " in part and part.split(": ", 1)[0] in STRATEGY:       # "momentum_breakout: close not above ..."
        part = part.split(": ", 1)[1]
    if part.startswith("error:"):
        return "Internal calculation error."
    ctx = {"tf": tf, "orb": orb_window}
    for rx, fn in _COMPILED:
        m = rx.search(part)
        if m:
            return fn(m, ctx)
    return part


def explain(reason: str | None, tf: str = "15m", orb_window: int = 120) -> str:
    if not reason:
        return ""
    out = []
    for p in str(reason).split("; "):
        t = explain_one(p, tf, orb_window)
        if t and t not in out:
            out.append(t)
    return " ".join(out)


def strategy(name: str | None) -> str:
    return STRATEGY.get(name or "", (name or "").replace("_", " ").capitalize())


def exit_reason(name: str | None) -> str:
    return EXIT.get(name or "", (name or "").replace("_", " ").capitalize())


def sentiment(score) -> tuple[str, str]:
    if score is None:
        return "neutral", "neutral"
    if score >= 0.15:
        return "positive", "good"
    if score <= -0.15:
        return "negative", "bad"
    return "neutral", "neutral"
