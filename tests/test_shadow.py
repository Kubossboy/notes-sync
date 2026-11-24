"""D91: shadow forward test - every strategy learns on a separate ledger; evidence-based promotion only."""
import sqlite3
from datetime import timedelta

import pytest

from app.execution.trader import PaperTrader
from app.shadow.engine import ShadowEngine, window_stats
from tests.test_risk_execution import NOW, FakeEngine, add_signal, bar, env, q  # noqa: F401


def _run(db, sid):
    rid = db.insert("scan_runs", {"started_ts": NOW.isoformat(), "scanner": "crypto", "mode": "live",
                                  "timeframe": "15m", "symbols": "BTC-USD", "config_hash": "t"})
    db.conn.execute("UPDATE signals SET scan_run_id=? WHERE id=?", (rid, sid))
    return [rid]


def _fake_trade(db, strategy, pnl, i, ac="crypto"):
    sid = add_signal(db, strategy=strategy, entry=100 + i * 0.01)
    pid = db.insert("shadow_positions", {
        "opened_ts": NOW.isoformat(), "signal_id": sid, "symbol": f"S{i}", "asset_class": ac, "strategy": strategy,
        "entry_price": 100, "stop_loss": 99, "take_profit": 103, "quantity": 2.5, "planned_risk_usd": 2.5,
        "entry_fee_usd": 0, "status": "closed", "closed_ts": NOW.isoformat(), "exit_price": 100, "pnl_usd": pnl,
        "r_multiple": pnl / 2.5})
    db.insert("shadow_trades", {"ts": NOW.isoformat(), "shadow_position_id": pid, "signal_id": sid, "symbol": f"S{i}",
                                "asset_class": ac, "strategy": strategy, "opened_ts": NOW.isoformat(),
                                "closed_ts": (NOW + timedelta(minutes=i)).isoformat(), "entry_price": 100,
                                "exit_price": 100, "quantity": 2.5, "planned_risk_usd": 2.5, "fees_usd": 0.1,
                                "pnl_usd": pnl, "r_multiple": pnl / 2.5, "exit_reason": "x"})


def test_failed_strategy_trades_in_shadow_only_never_touches_portfolio_or_alpaca(env):
    from tests.test_forward_test import SpyBroker
    broker = SpyBroker()
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine(), alpaca=broker)
    sid = add_signal(env.db, strategy="momentum_breakout", status="rejected", validation_status="failed")
    runs = _run(env.db, sid)
    opened = tr.shadow.open_from_signals(runs, lambda s, n: q(), NOW)
    assert len(opened) == 1 and broker.orders == []
    assert env.db.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0          # $1,000 ledger untouched
    assert env.db.conn.execute("SELECT COUNT(*) FROM risk_evaluations").fetchone()[0] == 0   # risk engine untouched
    assert tr.shadow.open_from_signals(runs, lambda s, n: q(), NOW) == []                   # one per signal / symbol
    tr.engine.bars = [bar(NOW + timedelta(minutes=1), 100, 103.6, 99.9, 103.2)]
    res = tr.shadow.manage(lambda pos: tr.engine.bars, NOW + timedelta(minutes=2))
    assert res[0]["reason"] == "take_profit" and res[0]["r"] > 2                              # costs included
    t = env.db.conn.execute("SELECT * FROM shadow_trades").fetchone()
    assert t["fees_usd"] > 0 and t["planned_risk_usd"] == pytest.approx(2.5)
    with pytest.raises(sqlite3.DatabaseError):
        env.db.conn.execute("DELETE FROM shadow_trades")
    with pytest.raises(sqlite3.DatabaseError):
        env.db.conn.execute("UPDATE shadow_positions SET pnl_usd=0")                         # closed = immutable
    assert env.db.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


def test_promotion_needs_30_trades_pf_and_expectancy_then_demotion(env):
    sh = ShadowEngine(env.s, env.db)
    for i in range(29):
        _fake_trade(env.db, "vwap_reclaim", 5.0 if i % 2 else -2.5, i)
    assert sh.evaluate(NOW) == [] and sh.status("vwap_reclaim", "crypto") == "shadow"         # 29 < 30
    _fake_trade(env.db, "vwap_reclaim", 5.0, 29)
    ch = sh.evaluate(NOW)
    assert ch and ch[0]["to"] == "eligible" and sh.status("vwap_reclaim", "crypto") == "eligible"
    for i in range(30, 52):
        _fake_trade(env.db, "vwap_reclaim", -2.5 if i % 4 else 2.0, i)                        # edge disappears
    ch = sh.evaluate(NOW)
    assert ch and ch[0]["to"] == "shadow"
    log = env.db.conn.execute("SELECT from_status, to_status FROM strategy_status_log ORDER BY id").fetchall()
    assert [tuple(r) for r in log] == [("shadow", "eligible"), ("eligible", "shadow")]
    with pytest.raises(sqlite3.DatabaseError):
        env.db.conn.execute("DELETE FROM strategy_status_log")


def test_no_promotion_on_low_pf_or_large_drawdown(env):
    sh = ShadowEngine(env.s, env.db)
    for i in range(40):
        _fake_trade(env.db, "rsi_mean_reversion", 3.0 if i % 2 else -2.5, i)                 # PF 1.2 < 1.3
    assert sh.evaluate(NOW) == []
    assert window_stats([{"pnl_usd": -60, "r_multiple": -24}])["max_drawdown_usd"] == 60


def test_promoted_strategy_passes_validation_gate_but_not_hard_limits(env):
    from tests.test_risk_execution import proposal
    sh = ShadowEngine(env.s, env.db)
    for i in range(30):
        _fake_trade(env.db, "momentum_breakout", 5.0 if i % 2 else -2.5, i)
    sh.evaluate(NOW)
    sid = add_signal(env.db, strategy="momentum_breakout", validation_status="shadow_promoted", score=76.0)
    d = env.risk.evaluate(proposal(env.db, sid), q(), NOW)
    assert d.approved, d.reason
    assert d.planned_risk_usd <= 2.5 + 1e-9
    sid2 = add_signal(env.db, strategy="ema_pullback", validation_status="shadow_promoted", entry=100.5, score=77.0)
    d2 = env.risk.evaluate(proposal(env.db, sid2), q(), NOW)                                 # not promoted -> blocked
    assert "strategy_validated_or_forward_test" in d2.reason


def test_dashboard_shows_shadow_section(settings):
    from app.dashboard.data import collect
    from app.database.db import Database
    d = Database(settings.db_path)
    d.migrate()
    d.close()
    from app.dashboard.server import render
    page = render(collect(settings, settings.db_path, NOW))
    assert "Strategie (stínový test)" in page and "0 / 30" in page and "No parameter tuning" in page


def test_shadow_errors_never_break_a_trading_cycle(env, monkeypatch):
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine())
    monkeypatch.setattr(tr.shadow, "manage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rep = tr.run_once(scan=False, now=NOW)
    assert rep["shadow"]["managed"][0]["error"].startswith("RuntimeError")
