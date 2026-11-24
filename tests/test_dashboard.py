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
              "No forward-test trades yet", "PAPER ONLY", "SAFE STATE: normal", "Alpaca paper mirror ON",
              "Not enough equity history yet", "<details>", "Balance (equity)", "Forward test (news)"):
        assert s in page
    assert "DEMO" not in page
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
    srv = serve(settings, "127.0.0.1", 0, settings.db_path, live_prices=False)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert urllib.request.urlopen(base + "/healthz").read() == b"ok"
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "ATLAS Trading AI" in html and "Strategy validation" in html and "Account balance" in html
        assert "Balance (equity)" in urllib.request.urlopen(base + "/fragment/top").read().decode()
        st = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert set(st) >= {"overview", "portfolio", "scanner", "analytics", "forward_test", "validation", "news",
                           "risk", "system"}
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/kill")
    finally:
        srv.shutdown()
        srv.server_close()


def test_localhost_only(settings):
    with pytest.raises(ValueError):
        serve(settings, "0.0.0.0", 0)


def _demo_pos(sym, entry):
    from datetime import datetime, timezone
    t0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    c = [(t0 + timedelta(minutes=15 * i), entry, entry * 1.002, entry * 0.998, entry * 1.001) for i in range(30)]
    return {"symbol": sym, "asset_class": "crypto", "strategy": "s", "validation_status": "forward_test",
            "quantity": 0.01, "entry_price": entry, "current_price": entry * 1.001, "mark_ts": None,
            "unrealized_pnl_usd": 0.1, "stop_loss": entry * 0.99, "take_profit": entry * 1.03, "opened_ts": None,
            "planned_risk_usd": 2.5, "candles": c, "chart_tf": "15m", "chart_source": "test"}


def test_position_grid_splits_for_two_positions(settings, dash_db):
    from app.dashboard.server import render_top
    d = collect(settings, settings.db_path, NOW)
    d["portfolio"] = [_demo_pos("BTC-USD", 100.0)]
    one = render_top(d)
    assert 'posgrid n1' in one and one.count("<svg") == 1 + 0          # one chart, equity has no svg yet
    d["portfolio"].append(_demo_pos("ETH-USD", 50.0))
    two = render_top(d)
    assert 'posgrid n2' in two and two.count('class="posbox"') == 2 and "stop" in two and "target" in two
    d["portfolio"][0]["candles"] = []
    assert "No price data available" in render_top(d)                   # never guesses a chart


def test_equity_curve_svg():
    from datetime import datetime, timezone
    from app.dashboard.charts import equity_svg
    t = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    assert "Not enough equity history" in equity_svg([(t, 1000.0)], 1000.0)
    svg = equity_svg([(t, 1000.0), (t + timedelta(hours=1), 997.5), (t + timedelta(hours=2), 1003.0)], 1000.0)
    assert svg.startswith("<svg") and "<path" in svg and "$1,000" in svg
