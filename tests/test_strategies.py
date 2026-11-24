from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.config import load_settings
from app.data.models import AssetClass
from app.data.orderbook import BookSnapshot
from app.scanners.scoring import build_plan, component_scores, liquidity_assessment, total_score
from app.strategies import crypto as C
from app.strategies import stock as S
from app.strategies.base import NoSetup, Setup, StrategyContext
from app.technical.indicators import compute_all, support_resistance

UTC = timezone.utc
SET = load_settings()


def bars(closes, start, step=300, vols=None, session=None, spread=0.05):
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        rows.append({"time": start + timedelta(seconds=step * i), "open": o, "high": max(o, c) + spread,
                     "low": min(o, c) - spread, "close": c,
                     "volume": vols[i] if vols is not None else 1000.0, "session": session})
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")


def ctx_for(df, ac=AssetClass.CRYPTO, book=None, history=None, cost=0.0, tf="15m"):
    d = compute_all(df, ac)
    return StrategyContext("BTC-USD" if ac == AssetClass.CRYPTO else "AAPL", ac, tf, d, df, support_resistance(d),
                           book, history or [], None, cost)


def uptrend_then_breakout(n=160, seed=2, jump=1.2, vol_mult=5.0):
    r = np.random.default_rng(seed)
    closes = list(100 + np.cumsum(r.normal(0.02, 0.15, n - 1)))
    closes.append(max(closes[-48:]) + jump)
    vols = [1000.0] * (n - 1) + [1000.0 * vol_mult]
    return bars(closes, datetime(2026, 11, 20, tzinfo=UTC), step=900, vols=vols, spread=0.1)


def book(bid_usd=1e6, ask_usd=1e6, mid=100.0):
    return BookSnapshot("BTC-USD", "coinbase", datetime.now(UTC),
                        [(mid - 0.01, bid_usd / mid)], [(mid + 0.01, ask_usd / mid)])


# ------------------------------------------------------------------ crypto
def test_crypto_momentum_breakout_triggers_on_real_breakout():
    ctx = ctx_for(uptrend_then_breakout())
    last = ctx.last
    if not (last["ema9"] > last["ema20"] > last["ema50"]):
        pytest.skip("synthetic path did not produce stacked EMAs")
    s = C.momentum_breakout(ctx, {"donchian": 48, "min_roc": 0.1, "min_rvol": 2.0, "rsi_max": 95})
    assert s.entry == pytest.approx(last["close"]) and s.stop < s.entry
    assert s.evidence["rvol"] >= 2.0


def test_crypto_momentum_breakout_rejects_without_volume():
    ctx = ctx_for(uptrend_then_breakout(vol_mult=1.0))
    with pytest.raises(NoSetup):
        C.momentum_breakout(ctx, {"donchian": 48, "min_roc": 0.1, "min_rvol": 2.0, "rsi_max": 95})


def test_cost_filter_blocks_low_volatility():
    ctx = ctx_for(uptrend_then_breakout(), cost=5000)  # absurd cost
    with pytest.raises(NoSetup, match="round-trip cost"):
        C.momentum_breakout(ctx, {})


def test_orderbook_imbalance():
    df = bars([100 + i * 0.05 for i in range(80)], datetime(2026, 11, 20, tzinfo=UTC), step=900)
    ctx = ctx_for(df, book=book(3e6, 1e6))
    s = C.orderbook_imbalance(ctx, {"min_imbalance": 0.3, "depth_pct": 0.5})
    assert s.evidence["imbalance"] == pytest.approx(0.5)
    with pytest.raises(NoSetup, match="imbalance"):
        C.orderbook_imbalance(ctx_for(df, book=book(1e6, 1e6)), {"min_imbalance": 0.3})
    with pytest.raises(NoSetup, match="no order book"):
        C.orderbook_imbalance(ctx_for(df), {})


def test_liquidity_change_needs_history_and_detects_thinning():
    df = bars([100 + i * 0.05 for i in range(80)], datetime(2026, 11, 20, tzinfo=UTC), step=900)
    hist = [{"bid_depth_1_usd": 1e6, "ask_depth_1_usd": 1e6}] * 6
    with pytest.raises(NoSetup, match="history"):
        C.liquidity_change(ctx_for(df, book=book(1e6, 3e5), history=hist[:2]), {"min_history": 5})
    s = C.liquidity_change(ctx_for(df, book=book(1e6, 3e5), history=hist), {"min_history": 5})
    assert s.evidence["ask_depth_drop_pct"] == pytest.approx(70)
    with pytest.raises(NoSetup, match="bid depth also fell"):
        C.liquidity_change(ctx_for(df, book=book(3e5, 3e5), history=hist), {"min_history": 5})
    assert "liquidity drop" in C.liquidity_alert(ctx_for(df, book=book(3e5, 3e5), history=hist))


def test_volatility_expansion_after_squeeze():
    flat = [100 + 0.01 * ((-1) ** i) for i in range(130)]
    noisy = list(100 + np.random.default_rng(5).normal(0, 0.8, 100))
    closes = noisy + flat + [101.5]
    df = bars(closes, datetime(2026, 11, 18, tzinfo=UTC), step=900, spread=0.02)
    s = C.volatility_expansion(ctx_for(df), {"squeeze_pctile": 20, "squeeze_lookback": 100})
    assert s.stop < s.entry


def test_crypto_volume_expansion():
    closes = [100 + 0.02 * i for i in range(80)] + [101.8, 102.3]
    vols = [1000.0] * 80 + [4000.0, 5000.0]
    s = C.volume_expansion(ctx_for(bars(closes, datetime(2026, 11, 20, tzinfo=UTC), 900, vols)),
                           {"min_rvol": 2.5, "bars": 2})
    assert len(s.evidence["rvol"]) == 2


# ------------------------------------------------------------------ stocks
def _stock_days():
    """3 prior regular sessions + today's first bars (EST: 09:30 ET = 14:30 UTC)."""
    frames = []
    for d in (19, 20, 23):
        frames.append(bars([100 + 0.01 * i for i in range(78)], datetime(2026, 11, d, 14, 30, tzinfo=UTC),
                           vols=[1000.0] * 78, session="regular"))
    return frames


def test_opening_range_breakout():
    prior = _stock_days()
    today = bars([101, 101.3, 100.9, 101.1, 101.6], datetime(2026, 11, 24, 14, 30, tzinfo=UTC),
                 vols=[1000.0, 1000.0, 1000.0, 1000.0, 4000.0], session="regular")
    df = pd.concat(prior + [today])
    ctx = ctx_for(df, AssetClass.STOCK, tf="5m")
    s = S.opening_range_breakout(ctx, {"or_minutes": 15, "max_minutes_after_open": 120, "min_rvol": 1.5})
    assert s.evidence["or_high"] == pytest.approx(101.35) and s.stop >= s.evidence["or_low"]
    late = bars([101] * 40 + [102], datetime(2026, 11, 24, 14, 30, tzinfo=UTC), session="regular")
    with pytest.raises(NoSetup, match="after open"):
        S.opening_range_breakout(ctx_for(pd.concat(prior + [late]), AssetClass.STOCK, tf="5m"), {})


def test_vwap_reclaim():
    prior = _stock_days()
    closes = [100, 100.5, 101, 100.8, 100.4, 100.1, 99.9, 99.8, 99.9, 100.0, 100.2, 101.0]
    vols = [3000.0] * 3 + [1000.0] * 8 + [3000.0]
    today = bars(closes, datetime(2026, 11, 24, 14, 30, tzinfo=UTC), vols=vols, session="regular")
    ctx = ctx_for(pd.concat(prior + [today]), AssetClass.STOCK, tf="5m")
    s = S.vwap_reclaim(ctx, {"min_bars_below": 3, "lookback": 6, "min_rvol": 1.2})
    assert s.entry > s.evidence["vwap"] > s.stop


def test_news_momentum_without_news_engine_is_no_setup():
    ctx = StrategyContext("NVDA", AssetClass.STOCK, "5m", pd.DataFrame(), news=None)
    with pytest.raises(NoSetup, match="news engine unavailable"):
        S.news_momentum(ctx, {})


def test_trend_continuation_requires_uptrend():
    dn = bars([200 - 0.1 * i for i in range(250)], datetime(2026, 11, 20, 14, 30, tzinfo=UTC), session="regular")
    with pytest.raises(NoSetup, match="trend is"):
        S.trend_continuation(ctx_for(dn, AssetClass.STOCK, tf="5m"), {})


# ------------------------------------------------------------------ planning & scoring
def _ctx_simple(levels=None, cost=0.0, atr=1.0):
    idx = pd.date_range("2026-11-24", periods=2, freq="15min", tz="UTC")
    df = pd.DataFrame({"open": [99, 100], "high": [100, 101], "low": [98, 99], "close": [99.5, 100],
                       "volume": [1, 1], "atr14": [atr, atr]}, index=idx)
    return StrategyContext("X-USD", AssetClass.CRYPTO, "15m", df, None, levels or [], None, [], None, cost)


def test_plan_reward_risk_and_costs():
    p = build_plan(Setup("t", "X", "long", 100, 99, 2.5), _ctx_simple(), SET)
    assert p.target == pytest.approx(102.5) and p.reward_risk_net == pytest.approx(2.5) and not p.problems
    p = build_plan(Setup("t", "X", "long", 100, 99, 2.5), _ctx_simple(cost=20), SET)  # 0.4 round trip
    assert p.reward_risk_net == pytest.approx((2.5 - 0.4) / (1 + 0.4), abs=1e-3)
    assert any("net reward/risk" in x for x in p.problems)


def test_plan_target_capped_by_resistance():
    lv = [{"price": 101.2, "touches": 3, "kinds": ["high"]}]
    p = build_plan(Setup("t", "X", "long", 100, 99, 2.5), _ctx_simple(lv), SET)
    assert p.target_capped_by == 101.2 and p.reward_risk_net < 2 and p.problems


@pytest.mark.parametrize("stop,needle", [(100.5, "invalid stop"), (99.9, "too tight"), (95, "too wide")])
def test_plan_stop_sanity(stop, needle):
    p = build_plan(Setup("t", "X", "long", 100, stop, 2.5), _ctx_simple(), SET)
    assert any(needle in x for x in p.problems)


def test_total_score_missing_components():
    comps = {"technical": 100, "momentum_volume": 50, "liquidity": 80, "news": None, "sentiment": None, "quant": None}
    total, avail, on_avail = total_score(comps, SET)
    assert total == pytest.approx(25 + 10 + 8)
    assert avail == pytest.approx(0.55) and on_avail == pytest.approx(43 / 0.55, abs=0.01)


def test_component_scores_and_liquidity():
    ctx = ctx_for(uptrend_then_breakout(), book=book(1e6, 1e6))
    comps = component_scores(ctx, liquidity_assessment(ctx, SET, ctx.book.spread_bps))
    assert comps["news"] is None and comps["quant"] is None
    assert all(0 <= comps[k] <= 100 for k in ("technical", "momentum_volume", "liquidity"))
    thin = ctx_for(uptrend_then_breakout(), book=book(1e3, 1e3))
    assert liquidity_assessment(thin, SET, None)["problems"]
