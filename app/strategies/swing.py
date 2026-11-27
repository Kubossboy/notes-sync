"""D103 Connors RSI(2) overnight swing for US stocks (Connors & Alvarez 2008, adapted and backtested in D100/D103).

Daily bars incl. today's forming bar (its close = the current price, read 15:45-15:59 New York):
  entry  : close > SMA200 and RSI(2) < 5 -> buy near the close, unlevered
  stop   : entry - 2 x ATR(10); a gap through the stop fills at the next regular-session open
  exits  : close back above SMA5 (checked near the close), or 10 trading days, or a 2.2 R profit cap
Pure functions; the trader does the I/O."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.data.calendar import ET, regular_close_at, regular_session_open

STRATEGY = "rsi2_swing"


def rsi(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def daily_frame(candles) -> pd.DataFrame:
    """Daily candles INCLUDING today's forming bar (to_frame drops it; here its close is the live price)."""
    df = pd.DataFrame([{"time": c.open_time, "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                        "volume": c.volume} for c in candles], columns=["time", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df.set_index("time")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time").sort_index()


def in_window(now: datetime, cfg) -> bool:
    """15:45 <= now < 15:59 New York on a regular session (early-close days use their own close)."""
    if not regular_session_open(now):
        return False
    close = regular_close_at(now)
    if close is None:
        return False
    mins = (close - now).total_seconds() / 60
    return cfg.last_minutes_before_close < mins <= cfg.entry_minutes_before_close


def ny_day(ts) -> object:
    return pd.Timestamp(ts).tz_convert(ET).date()


def why_not(df: pd.DataFrame | None, cfg) -> str:
    """Plain-English reason there is no RSI(2) setup today."""
    if df is None or len(df) < cfg.sma_long + 5:
        return "Overnight dip-buy: not enough daily history."
    c = df["close"].astype(float)
    r2, s200 = float(rsi(c, 2).iloc[-1]), float(c.rolling(cfg.sma_long).mean().iloc[-1])
    if not c.iloc[-1] > s200:
        return "Overnight dip-buy: price is below its 200-day average (no uptrend)."
    return f"Overnight dip-buy: not oversold enough (RSI(2) {r2:.0f}, needs below {cfg.rsi_max:g})."


def setup(df: pd.DataFrame, cfg) -> dict | None:
    """df: daily OHLC (UTC index), last row = today's bar so far. Returns the entry plan or None."""
    if df is None or len(df) < cfg.sma_long + 5:
        return None
    c = df["close"].astype(float)
    r2 = float(rsi(c, 2).iloc[-1])
    s200 = float(c.rolling(cfg.sma_long).mean().iloc[-1])
    a = float(atr(df, cfg.atr_period).iloc[-1])
    e = float(c.iloc[-1])
    if not (np.isfinite(r2) and np.isfinite(s200) and np.isfinite(a)) or a <= 0:
        return None
    if not (e > s200 and r2 < cfg.rsi_max):
        return None
    stop = e - cfg.atr_mult * a
    if stop <= 0:
        return None
    return {"entry": e, "stop": stop, "target": e + cfg.target_r * (e - stop), "rsi2": round(r2, 2),
            "sma200": round(s200, 4), "atr": round(a, 4),
            "why": (f"Oversold dip in an uptrend: RSI(2) {r2:.1f} (below {cfg.rsi_max:g}) while the price is above "
                    f"its 200-day average. Bought near the close and held overnight; sells when the close is back "
                    f"above the 5-day average, at the stop ({cfg.atr_mult:g}x the average daily range below entry), "
                    f"or after {cfg.max_hold_days} trading days.")}


def exit_reason(df: pd.DataFrame, opened: datetime, price: float, cfg) -> str | None:
    """Near-the-close exit check for an open swing position (today's close := current price)."""
    if df is None or len(df) < cfg.sma_exit + 1:
        return None
    d = df.copy()
    entry_day = ny_day(opened)
    today = ny_day(d.index[-1])
    if today <= entry_day:
        return None                                   # never exit on the entry day
    d.iloc[-1, d.columns.get_loc("close")] = price
    s5 = float(d["close"].astype(float).rolling(cfg.sma_exit).mean().iloc[-1])
    if price > s5:
        return "rsi2_sma5_exit"
    held = sum(1 for t in d.index if ny_day(t) > entry_day)
    if held >= cfg.max_hold_days:
        return "max_hold_days"
    return None


def expires(now: datetime, cfg) -> datetime:
    close = regular_close_at(now)
    return (close - timedelta(minutes=cfg.last_minutes_before_close)) if close else now
