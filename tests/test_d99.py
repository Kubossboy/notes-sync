"""D99: ATR exits, adaptive leverage, momentum-aware resistance cap, stock volume filter, error fixes."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.config import load_settings
from app.data.models import AssetClass
from app.risk import leverage as LV
from app.scanners.scoring import build_plan, is_momentum
from app.strategies.base import Setup, StrategyContext


def _ctx(ac=AssetClass.CRYPTO, rvol=1.0, breakout=False, levels=(), n=60):
    idx = pd.date_range("2026-11-02 15:00", periods=n, freq="15min", tz="UTC")
    close = np.full(n, 100.0)
    high = close + 0.5
    if breakout:
        close[-1] = 101.0
        high[-1] = 101.2
    df = pd.DataFrame({"open": close, "high": high, "low": close - 0.5, "close": close, "volume": 1.0,
                       "atr14": 1.0, "rvol20": rvol}, index=idx)
    return StrategyContext("X-USD" if ac == AssetClass.CRYPTO else "XYZ", ac, "15m", df, None, list(levels), None, [],
                           idx[-1].to_pydatetime(), 1.0)


def _setup(e=100.0, s=99.9):
    return Setup(strategy="sr_breakout", symbol="X-USD", direction="long", entry=e, stop=s, target_r=2.5,
                 evidence={})


@pytest.fixture
def S():
    return load_settings()


def test_atr_plan_replaces_tight_strategy_stop(S):
    p = build_plan(_setup(), _ctx(), S)
    assert p.stop == pytest.approx(100 - 1.5) and p.target == pytest.approx(102.0)
    assert not p.problems and p.reward_risk_net >= 1.2


def test_resistance_caps_without_momentum_but_not_with(S):
    lv = [{"price": 101.0, "touches": 3, "kinds": ["high"]}]
    capped = build_plan(_setup(), _ctx(levels=lv), S)
    assert capped.target == 101.0 and any("reward/risk" in x for x in capped.problems)
    ctx = _ctx(levels=lv, breakout=True, rvol=2.0)
    assert is_momentum(ctx, S)
    free = build_plan(_setup(), ctx, S)
    assert free.target == pytest.approx(102.0) and free.target_capped_by is None


def test_stock_needs_volume(S):
    low = build_plan(_setup(), _ctx(AssetClass.STOCK, rvol=0.9), S)
    assert any("rvol 0.90 too low" in x for x in low.problems)
    assert not any("rvol" in x for x in build_plan(_setup(), _ctx(AssetClass.STOCK, rvol=1.3), S).problems)


def test_adaptive_leverage_keeps_stop_inside_liquidation(S):
    for sl_pct in (0.001, 0.005, 0.01, 0.03):
        fill = 100.0
        stop = fill * (1 - sl_pct)
        L = LV.adaptive_leverage(S, fill, stop)
        assert 1 <= L <= 100
        liq = LV.liq_price(fill, L, S.execution.maintenance_margin_pct)
        if liq is not None:
            assert fill - stop <= S.risk.lev_sl_liq_fraction * (fill - liq) + 1e-9
    assert LV.adaptive_leverage(S, 100.0, 99.9) == 100          # tight stop -> full leverage allowed
    st, tg, L = LV.atr_levels(S, 100.2, 100.0, 98.5, 102.0)
    assert st == pytest.approx(98.7) and tg == pytest.approx(102.2) and L < 100


def test_losing_crypto_strategies_disabled(S):
    assert not S.scanner.crypto_strategies["trend_continuation"].enabled
    assert not S.scanner.crypto_strategies["orderbook_imbalance"].enabled
    assert S.risk.lev_min_reward_risk == 1.2


def test_empty_alpaca_book_is_not_an_error():
    from app.data.providers.base import DataFormatError, EmptyBook, ProviderError
    assert issubclass(EmptyBook, ProviderError) and not issubclass(EmptyBook, DataFormatError)


def test_dashboard_hides_resolved_and_old_errors(tmp_path):
    from app.database.db import Database
    from app.dashboard.data import _rows  # noqa: F401  (query lives in system section)
    db = Database(tmp_path / "t.sqlite3")
    db.migrate()
    now = datetime.now(timezone.utc)
    for ts, msg, res in ((now, "fresh", 0), (now - timedelta(hours=30), "old", 0), (now, "fixed", 1)):
        db.conn.execute("INSERT INTO errors (ts, module, error_type, message, traceback, resolved) VALUES (?,?,?,?,?,?)",
                        (ts.isoformat(), "x", "E", msg, "", res))
    db.conn.commit()
    import inspect
    import app.dashboard.data as D
    src = inspect.getsource(D)
    assert "COALESCE(resolved, 0) = 0 AND ts >= ?" in src
    rows = db.conn.execute("SELECT message FROM errors WHERE COALESCE(resolved, 0) = 0 AND ts >= ?",
                           ((now - timedelta(hours=24)).isoformat(),)).fetchall()
    assert [r[0] for r in rows] == ["fresh"]


def test_migration_resolves_fixed_errors(tmp_path):
    from app.database.db import Database
    db = Database(tmp_path / "m.sqlite3")
    db.migrate()
    db.conn.execute("DELETE FROM schema_migrations WHERE name='0012_resolve_fixed_errors.sql'")
    for m, t in (("alpaca: empty quote (bid/ask 0)", "DataFormatError"),
                 ("UNIQUE constraint failed: news_events.content_hash", "IntegrityError"),
                 ("table market_observations has no column named is_complete", "OperationalError"),
                 ("something else", "X")):
        db.conn.execute("INSERT INTO errors (ts, module, error_type, message, traceback, resolved) VALUES (?,?,?,?,?,0)",
                        ("2026-11-27T00:00:00+00:00", "m", t, m, ""))
    db.conn.commit()
    db.migrate()
    left = [r[0] for r in db.conn.execute("SELECT message FROM errors WHERE resolved=0")]
    assert left == ["something else"]
