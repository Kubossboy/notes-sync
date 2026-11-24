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
    assert 'name="robots" content="noindex' in page and "READ-ONLY SNAPSHOT" in page and "state.json" in page
    assert "/fragment/top" not in page and "<form" not in page and "method:'POST'" not in page
    import re
    assert all("data-tf=" in m for m in re.findall(r"<button[^>]*", page))   # only view-only timeframe buttons
    assert "X-Robots-Tag: noindex" in (out / "_headers").read_text()
    assert "Disallow: /" in (out / "robots.txt").read_text()
    assert "Not found" in (out / "index.html").read_text()      # root reveals nothing
    st = json.loads((site / "state.json").read_text())
    assert set(st) >= {"generated_ts", "overview", "positions", "equity_curve"}
    blob = "".join(p.read_text() for p in out.rglob("*") if p.is_file())
    for bad in (str(tmp_path), "/workspace", "/home/", "sqlite3", ".secrets"):
        assert bad not in blob


def test_static_page_has_live_price_layer(settings, dash_db, tmp_path):
    from datetime import datetime, timedelta, timezone
    from app.dashboard.charts import position_chart_svg
    page = (export(settings, tmp_path / "s", "slug", settings.db_path, live_prices=False) / "index.html").read_text()
    assert "api.exchange.coinbase.com/products/" in page and 'id="live"' in page and "SNAPSHOT prices" in page
    assert "no free live stock feed" in page                          # stocks stay snapshot, labelled
    t = datetime(2026, 11, 24, 9, tzinfo=timezone.utc)
    svg = position_chart_svg([(t + timedelta(minutes=15 * i), 100, 101, 99, 100.5) for i in range(5)], 100, 98, 105, 100.5)
    assert 'data-lo="' in svg and 'class="lcw"' in svg and 'class="lcb"' in svg and 'class="mk"' in svg
    assert svg.count('class="lcw"') == 1                              # only the last candle is live-updated


def test_static_page_has_timeframe_switcher(settings, dash_db, tmp_path):
    page = (export(settings, tmp_path / "s", "slug", settings.db_path, live_prices=False) / "index.html").read_text()
    for s in ("/candles?granularity=", "'1m': [60, 1]", "'4h': [3600, 4]", "'1D': [86400, 1]", "TF_DEFAULT = '15m'",
              "localStorage", "snapshot candles only"):
        assert s in page, s


def test_static_page_uses_vendored_lightweight_charts(settings, dash_db, tmp_path):
    site = export(settings, tmp_path / "s", "slug", settings.db_path, live_prices=False)
    page = (site / "index.html").read_text()
    assert (site / "lwc.js").stat().st_size > 100_000                 # vendored, no CDN
    assert '<script src="lwc.js"></script>' in page and "cdn" not in page.lower()
    assert "TradingView Lightweight Charts" in page and "https://www.tradingview.com/" in page   # Apache-2.0 attribution
    for s in ("createPriceLine", "CandlestickSeries", "LineSeries", "pinch: true", "id=\"eqsec\""):
        assert s in page, s
