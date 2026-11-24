import sqlite3

import pytest

from app.database.db import Database, utcnow

EXPECTED = {"system_events", "errors", "system_state", "config_snapshots", "integration_health",
            "market_observations", "news_events", "sentiment_results", "signals", "signal_scores",
            "strategy_decisions", "risk_checks", "order_requests", "simulated_fills", "positions",
            "equity_snapshots", "schema_migrations"}


def _order(db, coid="o-1", mode="paper", qty=1.0):
    return db.insert("order_requests", {
        "ts": utcnow(), "client_order_id": coid, "symbol": "AAPL", "asset_class": "stock",
        "side": "buy", "order_type": "market", "quantity": qty, "stop_loss": 99.0,
        "take_profit": 104.0, "execution_mode": mode, "provider": "internal_sim"})


def test_schema_created_and_migration_idempotent(db):
    assert EXPECTED <= set(db.tables())
    assert db.migrate() == []
    assert db.integrity_ok() == (True, "ok")


def test_audit_tables_are_append_only(db):
    sid = db.insert("signals", {"ts": utcnow(), "symbol": "BTC-USD", "asset_class": "crypto",
                                "strategy": "momentum_breakout", "direction": "long",
                                "status": "rejected"})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.conn.execute("DELETE FROM signals WHERE id=?", (sid,))
    eid = db.log_event("x", "test", "hello")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.conn.execute("DELETE FROM system_events WHERE id=?", (eid,))


def test_fills_immutable_and_orders_paper_only(db):
    oid = _order(db)
    fid = db.insert("simulated_fills", {"ts": utcnow(), "order_id": oid, "fill_price": 100.0,
                                        "quantity": 1.0, "fee_usd": 0.0, "slippage_usd": 0.05})
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.conn.execute("UPDATE simulated_fills SET fill_price=1 WHERE id=?", (fid,))
    with pytest.raises(sqlite3.IntegrityError):
        _order(db, coid="o-live", mode="live")


def test_duplicate_client_order_id_rejected(db):
    _order(db, "dup")
    with pytest.raises(sqlite3.IntegrityError):
        _order(db, "dup")


def test_position_requires_stop_and_target(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert("positions", {"opened_ts": utcnow(), "symbol": "AAPL", "asset_class": "stock",
                                "direction": "long", "quantity": 1, "entry_price": 100.0,
                                "stop_loss": None, "take_profit": 104.0})


def test_check_constraints(db):
    with pytest.raises(sqlite3.IntegrityError):
        _order(db, "neg", qty=-1)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert("market_observations", {"observed_ts": utcnow(), "symbol": "X",
                                          "asset_class": "stock", "source": "t",
                                          "data_status": "made_up"})


def test_transaction_rollback(db):
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.log_event("inside", "test", "will roll back")
            raise RuntimeError("boom")
    assert db.conn.execute("SELECT COUNT(*) FROM system_events WHERE event_type='inside'").fetchone()[0] == 0


def test_state_and_error_journal(db):
    db.set_state("k", {"a": 1}); db.set_state("k", {"a": 2})
    assert db.get_state("k") == {"a": 2}
    try:
        raise ValueError("bad data")
    except ValueError as e:
        db.log_error("test", e)
    r = db.conn.execute("SELECT error_type, traceback FROM errors").fetchone()
    assert r["error_type"] == "ValueError" and "bad data" in r["traceback"]


def test_rejects_sql_injection_identifiers(db):
    with pytest.raises(ValueError):
        db.insert("signals; DROP TABLE x", {"a": 1})


def test_persists_across_reopen(tmp_path):
    p = tmp_path / "p.sqlite3"
    d = Database(p); d.migrate(); d.log_event("e", "t", "persist me"); d.close()
    d2 = Database(p)
    assert d2.migrate() == []
    assert d2.conn.execute("SELECT message FROM system_events").fetchone()[0] == "persist me"
    d2.close()
