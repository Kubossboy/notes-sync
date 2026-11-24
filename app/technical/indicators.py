"""Technical indicators computed from real, COMPLETE candles only.

Every function is causal: the value at bar t uses bars <= t only (no look-ahead).
Tests verify this by appending future bars and checking past values are unchanged,
and compare against independent reference implementations / published examples.
"""
from __future__ import annotations

from datetime import timezone

import numpy as np
import pandas as pd

from app.data.calendar import ET
from app.data.models import AssetClass, Candle


def to_frame(candles: list[Candle]) -> pd.DataFrame:
    """Complete candles -> DataFrame indexed by UTC open time. Forming bars are dropped."""
    rows = [{"time": c.open_time, "open": c.open, "high": c.high, "low": c.low, "close": c.close,
             "volume": c.volume, "session": c.session} for c in candles if c.is_complete]
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "session"])
    if df.empty:
        return df.set_index("time")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time").sort_index()


# ---------------------------------------------------------------- moving averages
def ema(s: pd.Series, n: int) -> pd.Series:
    """EMA seeded with the SMA of the first n values (classic definition)."""
    out = pd.Series(np.nan, index=s.index, dtype=float)
    vals = s.to_numpy(dtype=float)
    if len(vals) < n:
        return out
    k = 2 / (n + 1)
    prev = vals[:n].mean()
    res = np.full(len(vals), np.nan)
    res[n - 1] = prev
    for i in range(n, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        res[i] = prev
    return pd.Series(res, index=s.index)


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _wilder(vals: np.ndarray, n: int) -> np.ndarray:
    """Wilder smoothing seeded with the simple mean of the first n values."""
    res = np.full(len(vals), np.nan)
    if len(vals) < n:
        return res
    prev = vals[:n].mean()
    res[n - 1] = prev
    for i in range(n, len(vals)):
        prev = (prev * (n - 1) + vals[i]) / n
        res[i] = prev
    return res


# ---------------------------------------------------------------- oscillators
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI (matches the StockCharts/Wilder worked example)."""
    d = close.diff().to_numpy()[1:]
    gains, losses = np.clip(d, 0, None), np.clip(-d, 0, None)
    ag, al = _wilder(gains, n), _wilder(losses, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
        r = 100 - 100 / (1 + rs)
    r = np.where((al == 0) & (ag > 0), 100.0, r)
    r = np.where((al == 0) & (ag == 0), 50.0, r)
    return pd.Series(np.concatenate([[np.nan], r]), index=close.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = pd.Series(np.nan, index=close.index)
    valid = line.dropna()
    if len(valid) >= signal:
        sig.loc[valid.index] = ema(valid, signal)
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0] if len(df) else np.nan
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return pd.Series(_wilder(true_range(df).to_numpy(dtype=float), n), index=df.index)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)   # population std (Bollinger's definition)
    up, lo = mid + k * sd, mid - k * sd
    return pd.DataFrame({"bb_mid": mid, "bb_upper": up, "bb_lower": lo,
                         "bb_width": (up - lo) / mid, "bb_pctb": (close - lo) / (up - lo)})


def roc(close: pd.Series, n: int = 10) -> pd.Series:
    return (close / close.shift(n) - 1) * 100


# ---------------------------------------------------------------- volume
def session_key(df: pd.DataFrame, asset_class: AssetClass) -> pd.Series:
    """VWAP/ORB session id: stocks = ET trading date (regular session only), crypto = UTC date."""
    if asset_class == AssetClass.STOCK:
        return pd.Series(df.index.tz_convert(ET).date, index=df.index)
    return pd.Series(df.index.tz_convert(timezone.utc).date, index=df.index)


def vwap(df: pd.DataFrame, asset_class: AssetClass) -> pd.Series:
    """Session-anchored VWAP. Stocks: regular-session bars only (pre/post -> NaN)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    mask = pd.Series(True, index=df.index)
    if asset_class == AssetClass.STOCK and "session" in df:
        mask = df["session"].isin(["regular"]) | df["session"].isna()
    key = session_key(df, asset_class)
    pv = (tp * df["volume"]).where(mask, 0.0)
    v = df["volume"].where(mask, 0.0)
    cum_pv = pv.groupby(key).cumsum()
    cum_v = v.groupby(key).cumsum()
    out = cum_pv / cum_v.replace(0, np.nan)
    return out.where(mask)


def relative_volume(vol: pd.Series, n: int = 20) -> pd.Series:
    """Volume vs the mean of the previous n bars (current bar excluded)."""
    base = vol.shift(1).rolling(n, min_periods=n).mean()
    return vol / base.replace(0, np.nan)


def relative_volume_tod(df: pd.DataFrame, min_days: int = 3) -> pd.Series:
    """Stocks: volume vs average volume of the same time-of-day slot on prior days."""
    et = df.index.tz_convert(ET)
    slot = pd.Series([t.strftime("%H:%M") for t in et], index=df.index)
    day = pd.Series(et.date, index=df.index)
    out = pd.Series(np.nan, index=df.index)
    for s, idx in slot.groupby(slot).groups.items():
        v = df.loc[idx, "volume"]
        prior_mean = v.shift(1).expanding(min_periods=min_days).mean()
        out.loc[idx] = v / prior_mean.replace(0, np.nan)
    return out


def volume_profile(df: pd.DataFrame, bins: int = 24, value_area: float = 0.70) -> dict | None:
    if df.empty or df["volume"].sum() <= 0:
        return None
    tp = (df["high"] + df["low"] + df["close"]) / 3
    lo, hi = df["low"].min(), df["high"].max()
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    hist, _ = np.histogram(tp, bins=edges, weights=df["volume"])
    poc_i = int(hist.argmax())
    order = np.argsort(hist)[::-1]
    total, acc, chosen = hist.sum(), 0.0, []
    for i in order:
        chosen.append(i); acc += hist[i]
        if acc >= value_area * total:
            break
    mids = (edges[:-1] + edges[1:]) / 2
    return {"poc": float(mids[poc_i]), "vah": float(edges[max(chosen) + 1]),
            "val": float(edges[min(chosen)]), "bins": bins}


# ---------------------------------------------------------------- structure
def pivots(df: pd.DataFrame, k: int = 3) -> tuple[pd.Series, pd.Series]:
    """Confirmed pivot highs/lows. A pivot at bar i needs k bars on both sides,
    so it only becomes known at bar i+k (no look-ahead when used as of the last bar)."""
    h, l = df["high"], df["low"]
    win = 2 * k + 1
    ph = h[(h == h.rolling(win, center=True).max())]
    pl = l[(l == l.rolling(win, center=True).min())]
    return ph.dropna(), pl.dropna()


def support_resistance(df: pd.DataFrame, k: int = 3, tolerance: float | None = None,
                       lookback: int = 300) -> list[dict]:
    """Cluster confirmed pivots into levels. Returns [{price, touches, kind, last_time}]."""
    d = df.tail(lookback)
    if len(d) < 2 * k + 2:
        return []
    ph, pl = pivots(d, k)
    if tolerance is None:
        a = atr(d).iloc[-1]
        tolerance = float(a) * 0.5 if a == a else float(d["close"].iloc[-1]) * 0.002
    pts = sorted([(float(p), "high", t) for t, p in ph.items()] + [(float(p), "low", t) for t, p in pl.items()])
    levels: list[dict] = []
    for p, kind, t in pts:
        if levels and abs(p - levels[-1]["price"]) <= tolerance:
            L = levels[-1]
            L["price"] = (L["price"] * L["touches"] + p) / (L["touches"] + 1)
            L["touches"] += 1
            L["kinds"].add(kind)
            L["last_time"] = max(L["last_time"], t)
        else:
            levels.append({"price": p, "touches": 1, "kinds": {kind}, "last_time": t})
    for L in levels:
        L["kinds"] = sorted(L["kinds"])
    return levels


def trend_state(df: pd.DataFrame) -> pd.Series:
    """'up' / 'down' / 'range' from EMA20 vs EMA50 and EMA20 slope over 5 bars."""
    e20, e50 = df["ema20"], df["ema50"]
    slope = e20 - e20.shift(5)
    up = (e20 > e50) & (slope > 0)
    dn = (e20 < e50) & (slope < 0)
    out = pd.Series("range", index=df.index)
    out[up] = "up"
    out[dn] = "down"
    out[e50.isna()] = None
    return out


def candle_patterns(df: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    upper = h - pd.concat([o, c], axis=1).max(axis=1)
    lower = pd.concat([o, c], axis=1).min(axis=1) - l
    po, pc = o.shift(1), c.shift(1)
    return pd.DataFrame({
        "pat_doji": body <= 0.1 * rng,
        "pat_hammer": (lower >= 2 * body) & (upper <= 0.3 * body.where(body > 0, rng * 0.1)) & (body > 0),
        "pat_shooting_star": (upper >= 2 * body) & (lower <= 0.3 * body.where(body > 0, rng * 0.1)) & (body > 0),
        "pat_bull_engulf": (pc < po) & (c > o) & (o <= pc) & (c >= po) & (body > (pc - po).abs()),
        "pat_bear_engulf": (pc > po) & (c < o) & (o >= pc) & (c <= po) & (body > (pc - po).abs()),
    }, index=df.index).fillna(False)


def breakout_up(df: pd.DataFrame, n: int = 20) -> pd.Series:
    prior_high = df["high"].shift(1).rolling(n, min_periods=n).max()
    return df["close"] > prior_high


# ---------------------------------------------------------------- all-in-one
def compute_all(df: pd.DataFrame, asset_class: AssetClass) -> pd.DataFrame:
    """Add all indicator columns. Input must contain complete bars only."""
    if df.empty:
        return df
    out = df.copy()
    c = out["close"]
    out["ema9"], out["ema20"], out["ema50"] = ema(c, 9), ema(c, 20), ema(c, 50)
    out["sma200"] = sma(c, 200)
    out["rsi14"] = rsi(c, 14)
    out = out.join(macd(c))
    out["atr14"] = atr(out, 14)
    out["atr_pct"] = out["atr14"] / c * 100
    out = out.join(bollinger(c))
    out["vwap"] = vwap(out, asset_class)
    out["rvol20"] = relative_volume(out["volume"], 20)
    if asset_class == AssetClass.STOCK:
        out["rvol_tod"] = relative_volume_tod(out)
    out["roc10"] = roc(c, 10)
    out["breakout20"] = breakout_up(out, 20)
    out["trend"] = trend_state(out)
    out = out.join(candle_patterns(out))
    return out
