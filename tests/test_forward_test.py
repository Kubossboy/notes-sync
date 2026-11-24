"""Part A: news-driven forward paper test, session-aware catalyst freshness, reweighted score."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from types import SimpleNamespace

from app.data.calendar import catalyst_fresh, first_session_close_after
from app.execution.trader import PaperTrader
from app.quant.engine import QuantEngine, forward_test_stats
from tests.test_risk_execution import FakeEngine, NOW, add_signal, bar, env, failed, proposal, q  # noqa: F401

UTC = timezone.utc


def test_session_aware_freshness_after_close_earnings():
    pub = datetime(2026, 11, 18, 21, 5, tzinfo=UTC)                 # Wed 16:05 ET, after the close
    assert first_session_close_after(pub) == datetime(2026, 11, 19, 21, 0, tzinfo=UTC)
    nxt_open = datetime(2026, 11, 19, 14, 35, tzinfo=UTC)           # Thu 09:35 ET (18.5 h later)
    assert catalyst_fresh(pub, nxt_open, 16, True)                   # flat 16 h would have expired
    assert not catalyst_fresh(pub, nxt_open, 16, False)
    assert not catalyst_fresh(pub, datetime(2026, 11, 19, 21, 30, tzinfo=UTC), 16, True)   # after that close
    fri = datetime(2026, 11, 20, 21, 10, tzinfo=UTC)                 # Fri after close -> Monday session
    assert catalyst_fresh(fri, datetime(2026, 11, 23, 15, 0, tzinfo=UTC), 16, True)
    assert not catalyst_fresh(fri, datetime(2026, 11, 23, 15, 0, tzinfo=UTC), 16, True, max_hours=48)
    assert not catalyst_fresh(nxt_open, pub, 16, True)               # never from the future


def test_quant_forward_test_state(settings, db):
    qe = QuantEngine(settings, db)
    st = qe.status(__import__("app.data.models", fromlist=["AssetClass"]).AssetClass.STOCK, "news_momentum")
    assert st["state"] == "forward_test" and "internal" in st["reason"] and "Alpaca PAPER" in st["reason"]
    from app.data.models import AssetClass
    assert qe.status(AssetClass.CRYPTO, "orderbook_imbalance")["state"] == "unavailable"   # not configured


def _row(env, sid):
    return env.db.conn.execute("SELECT s.*, sc.total_score FROM signals s JOIN signal_scores sc "
                               "ON sc.signal_id=s.id WHERE s.id=?", (sid,)).fetchone()


def test_risk_requires_validated_or_forward_test(env):
    for vs, strat, ok in (("failed", "momentum_breakout", False), ("unavailable", "momentum_breakout", False),
                          (None, "momentum_breakout", False), ("forward_test", "momentum_breakout", False),
                          ("forward_test", "news_momentum", True), ("validated", "momentum_breakout", True)):
        sid = add_signal(env.db, strategy=strat, validation_status=vs, entry=100 + len(str(vs)) * 0.001)
        d = env.risk.evaluate(proposal(env.db, sid), q(), NOW)
        assert ("strategy_validated_or_forward_test" not in failed(d)) == ok, (vs, strat)


class SpyBroker:
    """Stands in for AlpacaPaperBroker; records what would go to paper-api."""
    orders_enabled = True

    def __init__(self, tradable=True, fractionable=True):
        self.orders, self.closed = [], []
        self.info = SimpleNamespace(tradable=tradable, fractionable=fractionable, status="active")

    def asset(self, symbol):
        return self.info

    def submit_bracket(self, symbol, qty, stop, target, coid):
        self.orders.append(("bracket", symbol, qty))
        return {"id": "b1", "status": "accepted"}

    def submit_market(self, symbol, qty, coid, crypto):
        self.orders.append(("market", symbol, qty))
        return {"id": "m1", "status": "accepted"}

    def close_position(self, symbol):
        self.closed.append(symbol)
        return {"status": "submitted"}


def test_forward_test_trade_logged_separately_and_mirrored_to_paper(env):
    broker = SpyBroker()
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine(), alpaca=broker)
    sid = add_signal(env.db, strategy="news_momentum", validation_status="forward_test")
    res = tr.execute_signal(_row(env, sid), NOW)
    assert res["approved"], res["reason"]
    assert res["alpaca"]["mirrored"] is True
    kind, sym, qty = broker.orders[0]
    assert qty == pytest.approx(res["fill"]["quantity"])            # sized from the ATLAS $1,000 ledger
    assert qty * res["fill"]["fill_price"] <= 1000
    ext = env.db.conn.execute("SELECT external_order_id, external_status FROM order_requests WHERE id=?",
                              (res["fill"]["order_id"],)).fetchone()
    assert ext["external_order_id"] in ("b1", "m1") and ext["external_status"].startswith("alpaca_paper:")
    pos = env.db.conn.execute("SELECT * FROM positions WHERE id=?", (res["fill"]["position_id"],)).fetchone()
    assert pos["validation_status"] == "forward_test"
    assert forward_test_stats(env.db)["open_trades"] == 1
    tr.engine.bars = [bar(NOW + timedelta(minutes=1), 100, 100.1, 98.8, 98.9)]
    rep = tr.run_once(NOW + timedelta(minutes=2), scan=False)
    assert broker.closed == [sym]                                    # internal exit mirrored
    assert rep["managed"][0]["alpaca"]["mirrored"] is True
    st = forward_test_stats(env.db, "news_momentum")
    assert st["closed_trades"] == 1 and st["open_trades"] == 0 and st["net_pnl_usd"] < 0
    row = env.db.conn.execute("SELECT * FROM forward_test_trades").fetchone()
    assert row["exit_reason"] == "stop_loss" and row["planned_risk_usd"] <= 2.5
    with pytest.raises(sqlite3.DatabaseError):
        env.db.conn.execute("DELETE FROM forward_test_trades")


def test_mirror_skips_untradable_and_never_blocks_internal_trade(env):
    broker = SpyBroker(tradable=False)
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine(), alpaca=broker)
    res = tr.execute_signal(_row(env, add_signal(env.db)), NOW)
    assert res["approved"] and res["alpaca"]["mirrored"] is False and "not tradable" in res["alpaca"]["reason"]
    assert broker.orders == []
    tr.engine.bars = [bar(NOW + timedelta(minutes=1), 100, 100.1, 98.8, 98.9)]
    tr.run_once(NOW + timedelta(minutes=2), scan=False)
    assert broker.closed == []                                       # nothing mirrored -> nothing to close


def test_mirror_error_does_not_affect_ledger(env):
    class Boom(SpyBroker):
        def submit_bracket(self, *a, **k):
            raise ConnectionError("alpaca paper POST /v2/orders: HTTP 500")
        submit_market = submit_bracket
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine(), alpaca=Boom())
    res = tr.execute_signal(_row(env, add_signal(env.db)), NOW)
    assert res["approved"] and res["alpaca"]["mirrored"] is False
    assert env.db.conn.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0] == 1


def test_validated_trade_not_in_forward_log(env):
    tr = PaperTrader(env.s, env.db, env.safe, FakeEngine())
    sid = add_signal(env.db)
    tr.execute_signal(_row(env, sid), NOW)
    tr.engine.bars = [bar(NOW + timedelta(minutes=1), 100, 100.1, 98.8, 98.9)]
    tr.run_once(NOW + timedelta(minutes=2), scan=False)
    assert forward_test_stats(env.db)["closed_trades"] == 0


def test_m5_risk_limits_unchanged(settings):
    r, eq = settings.risk, settings.portfolio.starting_equity_usd
    assert eq * r.max_risk_per_trade_pct / 100 == pytest.approx(2.5)
    assert eq * r.max_daily_loss_pct / 100 == pytest.approx(10)
    assert r.max_open_positions == 2 and r.min_reward_risk == 2.0
    assert settings.execution.leverage_enabled is False
    assert settings.execution.alpaca_paper_orders_enabled is True     # D85: PAPER mirror, approved
    w = settings.scoring.weights
    assert w.news + w.sentiment >= 0.5 and w.technical + w.momentum_volume <= 0.2
