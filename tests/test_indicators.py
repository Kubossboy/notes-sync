from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.data.models import AssetClass, Candle
from app.technical import indicators as I

UTC = timezone.utc


def frame(closes, start=datetime(2026, 11, 24, 0, 0, tzinfo=UTC), step=300, vol=None, spread=0.5,
          session=None):
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        rows.append({"time": start + timedelta(seconds=step * i), "open": o, "high": max(o, c) + spread,
                     "low": min(o, c) - spread, "close": c, "volume": (vol[i] if vol else 100.0),
                     "session": session})
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")


def rng_walk(n=300, seed=1):
    r = np.random.default_rng(seed)
    return list(100 + np.cumsum(r.normal(0, 0.5, n)))


# ---- published / hand-computed reference values
WILDER_CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03,
                 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]


def test_rsi_matches_wilder_worked_example():
    r = I.rsi(pd.Series(WILDER_CLOSES), 14)
    assert r.iloc[:14].isna().all()
    # exact: avg gain 3.34/14, avg loss 1.40/14 -> RS 2.3857 -> 70.46
    assert r.iloc[14] == pytest.approx(100 - 100 / (1 + (3.34 / 14) / (1.40 / 14)), abs=1e-9)
    # StockCharts publishes 70.53, 66.32, 66.55, 69.41, 66.36, 57.97 (computed with rounded averages)
    published = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97]
    assert np.allclose(r.iloc[14:].to_numpy(), published, atol=0.1)


def test_rsi_bounds_and_flat():
    assert I.rsi(pd.Series([1.0] * 30)).iloc[-1] == 50
    assert I.rsi(pd.Series(range(1, 40), dtype=float)).iloc[-1] == 100
    r = I.rsi(pd.Series(rng_walk()))
    assert r.dropna().between(0, 100).all()


def test_ema_sma_against_reference():
    s = pd.Series(rng_walk(100))
    e = I.ema(s, 10)
    ref = [np.nan] * 9 + [s.iloc[:10].mean()]
    for x in s.iloc[10:]:
        ref.append(x * 2 / 11 + ref[-1] * 9 / 11)
    assert np.allclose(e.to_numpy(), ref, equal_nan=True)
    seeded = pd.concat([pd.Series([s.iloc[:10].mean()]), s.iloc[10:]]).ewm(span=10, adjust=False).mean()
    assert np.allclose(e.iloc[9:].to_numpy(), seeded.to_numpy())
    assert np.allclose(I.sma(s, 5).iloc[4:], [s.iloc[i - 4:i + 1].mean() for i in range(4, 100)])


def test_macd_definition():
    s = pd.Series(rng_walk(200))
    m = I.macd(s)
    assert np.allclose((I.ema(s, 12) - I.ema(s, 26)).dropna(), m["macd"].dropna())
    assert m["macd_signal"].first_valid_index() == 25 + 8
    assert np.allclose(m["macd_hist"].dropna(), (m["macd"] - m["macd_signal"]).dropna())


def test_atr_against_naive_wilder():
    df = frame(rng_walk(60))
    a = I.atr(df, 14)
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, 60)]
    ref = [np.nan] * 13 + [np.mean(tr[:14])]
    for t in tr[14:]:
        ref.append((ref[-1] * 13 + t) / 14)
    assert np.allclose(a.to_numpy(), ref, equal_nan=True)


def test_bollinger_population_std():
    s = pd.Series(rng_walk(50))
    b = I.bollinger(s, 20, 2)
    w = s.iloc[-20:].to_numpy()
    assert b["bb_upper"].iloc[-1] == pytest.approx(w.mean() + 2 * w.std())
    assert b["bb_lower"].iloc[-1] == pytest.approx(w.mean() - 2 * w.std())


def test_vwap_session_reset_crypto():
    df = frame([10, 11, 12, 13], start=datetime(2026, 11, 23, 23, 50, tzinfo=UTC), vol=[1, 2, 3, 4])
    v = I.vwap(df, AssetClass.CRYPTO)
    tp = ((df["high"] + df["low"] + df["close"]) / 3).to_numpy()
    assert v.iloc[1] == pytest.approx((tp[0] * 1 + tp[1] * 2) / 3)
    assert v.iloc[2] == pytest.approx(tp[2])            # 00:00 UTC -> new session
    assert v.iloc[3] == pytest.approx((tp[2] * 3 + tp[3] * 4) / 7)


def test_vwap_stock_regular_session_only():
    df = frame([10, 11, 12], start=datetime(2026, 11, 23, 14, 25, tzinfo=UTC), vol=[50, 2, 3])
    df["session"] = ["pre", "regular", "regular"]
    v = I.vwap(df, AssetClass.STOCK)
    tp = ((df["high"] + df["low"] + df["close"]) / 3).to_numpy()
    assert np.isnan(v.iloc[0]) and v.iloc[1] == pytest.approx(tp[1])


def test_relative_volume_excludes_current_bar():
    vol = pd.Series([100.0] * 20 + [300.0])
    assert I.relative_volume(vol, 20).iloc[-1] == pytest.approx(3.0)
    assert np.isnan(I.relative_volume(vol, 20).iloc[19])


def test_relative_volume_time_of_day():
    days = [datetime(2026, 11, d, 14, 30, tzinfo=UTC) for d in (16, 17, 18, 19, 20)]
    rows = [{"time": t, "open": 1, "high": 1, "low": 1, "close": 1, "volume": v, "session": "regular"}
            for t, v in zip(days, [100, 100, 100, 100, 250])]
    df = pd.DataFrame(rows).set_index("time")
    r = I.relative_volume_tod(df, min_days=3)
    assert np.isnan(r.iloc[2]) and r.iloc[-1] == pytest.approx(2.5)


def test_volume_profile_poc():
    closes = [100] * 30 + [110] * 5
    df = frame(closes, spread=0.1, vol=[10] * 30 + [1] * 5)
    vp = I.volume_profile(df, bins=10)
    assert abs(vp["poc"] - 100) < 1.2 and vp["val"] <= 100 <= vp["vah"]


def test_support_resistance_clusters_pivots():
    closes = []
    for _ in range(4):
        closes += [100, 102, 104, 106, 108, 110, 108, 106, 104, 102]
    df = frame(closes, spread=0.2)
    levels = I.support_resistance(df, k=3)
    res = [L for L in levels if "high" in L["kinds"] and L["touches"] >= 3]
    assert res and abs(res[0]["price"] - 110.2) < 0.5


def test_candle_patterns():
    rows = [  # bearish bar, then bullish engulfing
        {"open": 10, "high": 10.2, "low": 9.4, "close": 9.5},
        {"open": 9.4, "high": 10.6, "low": 9.3, "close": 10.5},
        {"open": 10, "high": 10.05, "low": 9.0, "close": 10.04},   # hammer
        {"open": 10, "high": 10.5, "low": 9.5, "close": 10.01},    # doji
    ]
    df = pd.DataFrame(rows, index=pd.date_range("2026-11-24", periods=4, freq="5min", tz="UTC"))
    p = I.candle_patterns(df)
    assert p["pat_bull_engulf"].iloc[1] and p["pat_hammer"].iloc[2] and p["pat_doji"].iloc[3]
    assert not p["pat_bear_engulf"].any()


def test_to_frame_drops_forming_bar():
    t = datetime(2026, 11, 24, tzinfo=UTC)
    cs = [Candle("X", AssetClass.CRYPTO, "s", "5m", t + timedelta(minutes=5 * i), 1, 1, 1, 1, 1,
                 is_complete=i < 2) for i in range(3)]
    assert len(I.to_frame(cs)) == 2


def test_no_lookahead_in_compute_all():
    """Every indicator value at bar t must be identical whether or not later bars exist."""
    closes = rng_walk(320, seed=7)
    full = I.compute_all(frame(closes, vol=list(np.random.default_rng(3).integers(50, 500, 320))),
                         AssetClass.CRYPTO)
    for cut in (210, 260, 300):
        part = I.compute_all(frame(closes[:cut], vol=list(np.random.default_rng(3).integers(50, 500, 320))[:cut]),
                             AssetClass.CRYPTO)
        a = full.iloc[:cut]
        for col in part.columns:
            if part[col].dtype.kind in "fi":
                assert np.allclose(a[col].to_numpy(dtype=float), part[col].to_numpy(dtype=float),
                                   equal_nan=True), col
            else:
                assert (a[col].fillna("NA").astype(str).to_numpy() == part[col].fillna("NA").astype(str).to_numpy()).all(), col


def test_levels_use_confirmed_pivots_only():
    closes = [100 + i for i in range(30)]           # rising: the latest high is NOT yet a pivot
    lv = I.support_resistance(frame(closes, spread=0.1), k=3)
    assert all(L["price"] < closes[-1] - 2 for L in lv if "high" in L["kinds"])


def test_trend_state():
    up = I.compute_all(frame([100 + i * 0.3 for i in range(80)]), AssetClass.CRYPTO)
    dn = I.compute_all(frame([100 - i * 0.3 for i in range(80)]), AssetClass.CRYPTO)
    assert up["trend"].iloc[-1] == "up" and dn["trend"].iloc[-1] == "down"
    assert pd.isna(up["trend"].iloc[10])   # not enough history for EMA50
