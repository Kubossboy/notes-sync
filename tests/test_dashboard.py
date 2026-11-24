"""Milestone 7: read-only dashboard."""
import json
import sqlite3
import threading
import urllib.request
from datetime import timedelta

import pytest

from app.dashboard.data import ReadOnlyDB, collect
from app.dashboard.server import render, serve
from app.database.db import Database
from app.execution.trader import PaperTrader
from tests.test_risk_execution import FakeEngine, NOW, add_signal, bar, env  # noqa: F401


@pytest.fixture
def dash_db(settings):
    d = Database(settings.db_path)
    d.migrate()
    yield d
    d.close()


def test_empty_state_is_labelled_not_faked(settings, dash_db):
    data = collect(settings, settings.db_path, NOW)
    o = data["overview"]
    assert o["equity"] == 1000 and o["open_positions"] == 0 and o["daily_loss_limit"] == 10
    assert o["label"].startswith("SIMULATED") and data["system"]["label"].startswith("REAL")
    page = render(data)
    for s in ("No journaled signals yet", "No open positions", "No closed paper trades yet",
              "No forward-test trades yet", "PAPER TRADING ONLY", "SAFE STATE: normal", "Alpaca orders OFF"):
        assert s in page
    # the dashboard must not have written anything (day-start equity stays in an in-memory overlay)
    assert dash_db.conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 0


def test_readonly_connection_cannot_write(settings, dash_db):
    ro = ReadOnlyDB(settings.db_path)
    with pytest.raises(sqlite3.OperationalError):
        ro.conn.execute("INSERT INTO system_state(key, value_json, updated_ts) VALUES ('x','1','t')")
    ro.close()


def test_sections_with_real_journal_rows(settings):
    db = Database(settings.db_path)
    db.migrate()
    from app.core.safety import SafeState
    tr = PaperTrader(settings, db, SafeState(db), FakeEngine())
    sid = add_signal(db, strategy="news_momentum", validation_status="forward_test")
    row = db.conn.execute("SELECT s.*, sc.total_score FROM signals s JOIN signal_scores sc ON sc.signal_id=s.id "
                          "WHERE s.id=?", (sid,)).fetchone()
    assert tr.execute_signal(row, NOW)["approved"]
    data = collect(settings, settings.db_path, NOW + timedelta(minutes=1))
    assert data["portfolio"][0]["symbol"] == "BTC-USD" and data["portfolio"][0]["validation_status"] == "forward_test"
    assert data["scanner"][0]["strategy"] == "news_momentum"
    assert data["forward_test"]["stats"]["open_trades"] == 1 and data["risk"][0]["approved"] == 1
    tr.engine.bars = [bar(NOW + timedelta(minutes=1), 100, 100.1, 98.8, 98.9)]
    tr.run_once(NOW + timedelta(minutes=2), scan=False)
    data = collect(settings, settings.db_path, NOW + timedelta(minutes=3))
    assert data["analytics"]["all"]["trades"] == 1 and data["forward_test"]["stats"]["closed_trades"] == 1
    assert "news_momentum" in data["analytics"]["by_strategy"] and "crypto" in data["analytics"]["by_asset_class"]
    assert "stop_loss" in render(data)
    db.close()


def test_http_endpoints(settings, dash_db):
    srv = serve(settings, "127.0.0.1", 0, settings.db_path)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert urllib.request.urlopen(base + "/healthz").read() == b"ok"
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "ATLAS Trading AI" in html and "Strategy validation" in html and "News intelligence" in html
        st = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert set(st) >= {"overview", "portfolio", "scanner", "analytics", "forward_test", "validation", "news",
                           "risk", "system"}
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/kill")
    finally:
        srv.shutdown()
        srv.server_close()
