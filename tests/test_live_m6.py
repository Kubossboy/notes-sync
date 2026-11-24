"""Milestone 6 live tests (pytest -m live): real historical bars from the free sources, then the
backtester on them. Nothing is persisted to the main database; no orders anywhere."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.backtest.engine import Backtester, metrics
from app.backtest.history import alpaca_history, coinbase_history, with_sessions, yahoo_history
from app.backtest.validation import strategy_params
from app.config import load_secrets
from app.data.models import AssetClass
from app.technical.indicators import compute_all

pytestmark = pytest.mark.live
REAL = load_secrets()
UTC = timezone.utc


def _check(df, step):
    assert len(df) > 50 and df.index.is_monotonic_increasing and not df.index.duplicated().any()
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all() and (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    assert df.index[-1].to_pydatetime() + timedelta(seconds=step) <= datetime.now(UTC)   # complete bars only


def test_coinbase_history_and_backtest(settings):
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    df = coinbase_history("BTC-USD", "15m", end - timedelta(days=8), end)
    _check(df, 900)
    gaps = (df.index.to_series().diff().dt.total_seconds() > 900).sum()
    assert gaps < 10
    k = len(df) - 40
    a, b = compute_all(df, AssetClass.CRYPTO).iloc[:k], compute_all(df.iloc[:k], AssetClass.CRYPTO)
    assert np.allclose(a["ema50"].to_numpy(float), b["ema50"].to_numpy(float), equal_nan=True)
    bt = Backtester(settings)
    ind = bt.prepare(df, AssetClass.CRYPTO)
    p = strategy_params(settings, AssetClass.CRYPTO)
    cands = bt.candidates(ind, "BTC-USD", AssetClass.CRYPTO, "15m",
                          {n: {"default": p[n]} for n in ("momentum_breakout", "trend_continuation")},
                          diag_min_rr=1.0)
    trades = bt.simulate({"BTC-USD": ind}, [c for c in cands if c.gated], AssetClass.CRYPTO, 900)
    assert metrics(trades)["trades"] == len(trades)
    assert sum(bt.diag.get((n, "default"), {}).get("raw_setups", 0) for n in ("momentum_breakout", "trend_continuation")) >= 0


@pytest.mark.skipif(not (REAL.has("alpaca_api_key_id") and REAL.has("alpaca_api_secret_key")),
                    reason="needs_credentials: Alpaca keys")
def test_alpaca_iex_history():
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    df = with_sessions(alpaca_history("SPY", "5m", end - timedelta(days=10), end, REAL))
    _check(df, 300)
    reg = df[df.session == "regular"]
    assert len(reg) >= 78 * 3
    per_day = reg.groupby(reg.index.tz_convert("America/New_York").date).size()
    assert per_day.max() <= 78


def test_yahoo_history_60d():
    df = with_sessions(yahoo_history("SPY", "5m"))
    _check(df, 300)
    assert (df.index.to_series().dt.minute % 5 == 0).all()
    assert (df.session == "regular").sum() > 78 * 30
