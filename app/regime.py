"""D100 market regime (daily bars of SPY for stocks, BTC-USD for crypto), two simple axes:
- trend: bull = close > SMA50 and SMA50 rising over 10 days; bear = close < SMA50 and SMA50 falling; else sideways
- volatility: ATR14 / close vs its own last 252 days -> high if at or above the median, else low
Computed on COMPLETED daily bars only (no look-ahead). The regime x strategy matrix (config) says which
strategies may open new trades in each regime."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRENDS = ("bull", "bear", "sideways")
VOLS = ("high", "low")
LABEL = {"bull": "rising (bullish trend)", "bear": "falling (bearish trend)", "sideways": "sideways",
         "high": "high volatility", "low": "low volatility"}


def regime_series(daily: pd.DataFrame, sma: int = 50, slope_days: int = 10, vol_lookback: int = 252) -> pd.DataFrame:
    """Regime per daily bar, valid for the NEXT day (i.e. use the row of the last completed bar)."""
    c = daily["close"].astype(float)
    m = c.rolling(sma).mean()
    slope = m / m.shift(slope_days) - 1
    pc = c.shift()
    tr = pd.concat([daily["high"] - daily["low"], (daily["high"] - pc).abs(), (daily["low"] - pc).abs()], axis=1).max(axis=1)
    natr = tr.rolling(14).mean() / c
    pct = natr.rolling(vol_lookback, min_periods=60).apply(lambda w: (w[:-1] < w[-1]).mean() if len(w) > 1 else np.nan,
                                                          raw=True)
    trend = np.where((c > m) & (slope > 0), "bull", np.where((c < m) & (slope < 0), "bear", "sideways"))
    trend = pd.Series(trend, index=daily.index).where(m.notna())
    vol = pd.Series(np.where(pct >= 0.5, "high", "low"), index=daily.index).where(pct.notna())
    return pd.DataFrame({"trend": trend, "vol": vol, "natr_pct": pct, "sma50": m, "slope": slope, "close": c})


def current(daily: pd.DataFrame, now=None) -> dict | None:
    """Regime from the last COMPLETED daily bar (drops a bar that is still forming at `now`)."""
    if daily is None or len(daily) < 70:
        return None
    d = daily
    if now is not None:
        ts = pd.Timestamp(now)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
        d = d[d.index + pd.Timedelta(days=1) <= ts] if len(d) else d
    r = regime_series(d).dropna(subset=["trend", "vol"])
    if r.empty:
        return None
    last = r.iloc[-1]
    return {"trend": last["trend"], "vol": last["vol"], "as_of": r.index[-1].isoformat(),
            "natr_pct": round(float(last["natr_pct"]), 2), "slope_pct": round(float(last["slope"]) * 100, 2),
            "text": f"{LABEL[last['trend']].capitalize()}, {LABEL[last['vol']]}"}


def allowed(matrix: dict, asset: str, strategy: str, reg: dict | None) -> bool:
    """matrix[asset][strategy] = list of 'trend/vol' cells. No regime known -> allowed (fail-open on data only);
    a strategy missing from the matrix -> allowed (matrix covers what was tested)."""
    if not reg:
        return True
    cells = (matrix.get(asset) or {}).get(strategy)
    if cells is None:
        return True
    return f"{reg['trend']}/{reg['vol']}" in cells
