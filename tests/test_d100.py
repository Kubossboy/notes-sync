"""D100: market regime, regime x strategy matrix, stock watch-only, plain-English reasons, dashboard card."""
import numpy as np
import pandas as pd

from app.config import load_settings
from app.dashboard.live import regime_view
from app.dashboard.plain import explain
from app.regime import allowed, current, regime_series


def _daily(trend: float, vol: float, n=400, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")
    steps = trend + rng.normal(0, 1, n) * np.r_[np.full(n - 60, 0.5), np.full(60, vol)]
    c = 1000 + np.cumsum(steps)
    return pd.DataFrame({"open": c, "high": c + abs(steps), "low": c - abs(steps), "close": c, "volume": 1.0}, index=idx)


def test_trend_and_vol_axes():
    up = current(_daily(0.3, 2.0), pd.Timestamp("2027-01-01", tz="UTC"))
    assert up["trend"] == "bull" and up["vol"] == "high"
    down = current(_daily(-0.3, 0.1), pd.Timestamp("2027-01-01", tz="UTC"))
    assert down["trend"] == "bear" and down["vol"] == "low"


def test_no_lookahead_drops_forming_bar():
    d = _daily(0.3, 1.0)
    r_all = current(d, d.index[-1] + pd.Timedelta(days=1))
    r_cut = current(d, d.index[-1] + pd.Timedelta(hours=12))      # last bar still forming
    assert r_all["as_of"] == d.index[-1].isoformat() and r_cut["as_of"] == d.index[-2].isoformat()
    assert regime_series(d)["trend"].iloc[:49].isna().all()


def test_matrix_gate():
    m = {"crypto": {"sr_breakout": ["bear/high"]}, "stock": {"vwap_reclaim": []}}
    assert allowed(m, "crypto", "sr_breakout", {"trend": "bear", "vol": "high"})
    assert not allowed(m, "crypto", "sr_breakout", {"trend": "sideways", "vol": "high"})
    assert not allowed(m, "stock", "vwap_reclaim", {"trend": "bull", "vol": "low"})
    assert allowed(m, "crypto", "untested", {"trend": "bull", "vol": "low"})
    assert allowed(m, "crypto", "sr_breakout", None)                   # regime unknown -> not gated


def test_default_config_matrix_and_watch_only():
    s = load_settings()
    assert s.regime.enabled and s.regime.stock_watch_only
    assert set(s.regime.matrix["crypto"]["momentum_breakout"]) == {"bear/high", "bull/low"}
    assert all(v == [] for k, v in s.regime.matrix["stock"].items() if k != "rsi2_swing")   # D103: only the swing


def test_plain_english_reasons():
    assert explain("regime sideways/high: strategy off in this market") == \
        "Skipped: this strategy is off in sideways, high-volatility markets."
    assert explain("watch-only: stock entries paused (no stock strategy had a tested edge)").startswith("Watch only")


def test_dashboard_regime_card():
    s = load_settings()
    v = regime_view(s, {"regime_crypto": {"trend": "bear", "vol": "high", "text": "Falling (bearish trend), high volatility",
                                          "benchmark": "BTC-USD"},
                        "regime_stock": {"trend": "bull", "vol": "low", "text": "x", "benchmark": "SPY"}})
    st, cr = v
    assert st["market"] == "Stocks" and st["allowed"].startswith("Watch only")
    assert cr["tone"] == "bad" and "Momentum breakout" in cr["allowed"]
    assert regime_view(s, {})[0]["text"] == "Not measured yet"
