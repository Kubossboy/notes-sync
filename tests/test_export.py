import json

import pytest

from app.database.db import Database

from app.dashboard.export import RED, export, publish_config, scrub


@pytest.fixture
def dash_db(settings):
    d = Database(settings.db_path)
    d.migrate()
    yield d


def test_scrub_redacts_paths_emails_tokens_and_secrets():
    s = scrub({"a": ["error at /workspace/x/var/atlas.sqlite3 line 3", "mail me@example.com",
                     "tok abcdefABCDEF0123456789abcdefABCDEF01", "https://x.io/a?key=1", "hunter2secret"],
               "n": 1.5}, secrets_=["hunter2secret"])
    assert s["n"] == 1.5
    assert all(RED in v for v in s["a"])
    assert "/workspace" not in json.dumps(s) and "example.com" not in json.dumps(s)
    assert scrub("DEMO_news_momentum BTC-USD 15m") == "DEMO_news_momentum BTC-USD 15m"


def test_export_static_snapshot(settings, dash_db, tmp_path):
    cfg = publish_config(settings)
    assert cfg == publish_config(settings) and len(cfg["slug"]) >= 20 and cfg["project"].startswith("atlas-")
    out = tmp_path / "site"
    site = export(settings, out, cfg["slug"], settings.db_path, live_prices=False)
    page = (site / "index.html").read_text()
    assert 'name="robots" content="noindex' in page and "data.json" in page and 'lang="cs"' in page
    assert "<form" not in page and "POST" not in page
    assert "X-Robots-Tag: noindex" in (out / "_headers").read_text()
    assert "Disallow: /" in (out / "robots.txt").read_text()
    assert "Not found" in (out / "index.html").read_text()      # root reveals nothing
    st = json.loads((site / "state.json").read_text())
    assert set(st) >= {"generated_ts", "overview", "positions"}
    view = json.loads((site / "data.json").read_text())
    assert set(view) >= {"summary", "positions", "trades", "signals", "near_misses", "scan_log", "universe", "tv_symbols"}
    assert view["universe"]["stocks"] == len(settings.markets.stocks.watchlist)
    blob = "".join(p.read_text() for p in out.rglob("*") if p.is_file())
    for bad in (str(tmp_path), "/workspace", "/home/", "sqlite3", ".secrets"):
        assert bad not in blob


def test_publish_config_from_env(settings, monkeypatch):
    monkeypatch.setenv("ATLAS_PUBLISH_PROJECT", "atlas-fixed")
    monkeypatch.setenv("ATLAS_PUBLISH_SLUG", "s" * 24)
    assert publish_config(settings) == {"project": "atlas-fixed", "slug": "s" * 24}


def test_dashboard_keeps_tradingview_widget(settings, dash_db, tmp_path):
    site = export(settings, tmp_path / "s", "slug", settings.db_path, live_prices=False)
    page = (site / "index.html").read_text()
    assert "https://s3.tradingview.com/tv.js" in page and "new TradingView.widget" in page and 'id="sym"' in page
    assert (site / "lwc.js").stat().st_size > 100_000 and "<details" in page and "api.bybit.com/v5/market/kline" in page
    view = json.loads((site / "data.json").read_text())
    assert any(x["tv"] == "BINANCE:BTCUSDT" for x in view["tv_symbols"])


def test_export_refuses_template_without_tradingview(settings, dash_db, tmp_path, monkeypatch):
    import app.dashboard.export as ex
    bad = tmp_path / "old.html"
    bad.write_text("<html>old chart</html>")
    monkeypatch.setattr(ex, "TEMPLATE", bad)
    with pytest.raises(RuntimeError):
        export(settings, tmp_path / "s", "slug", settings.db_path, live_prices=False)
