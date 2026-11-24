"""Milestone 4 live tests: real news / SEC ingestion (pytest -m live). Uses a temp DB."""
from datetime import datetime, timedelta, timezone

import pytest
import requests

from app.config import load_secrets
from app.core.bootstrap import bootstrap
from app.data.engine import MarketDataEngine
from app.data.models import AssetClass
from app.intelligence.engine import NewsEngine
from app.intelligence.feeds import fetch_feed
from app.intelligence.sec import SecClient
from app.intelligence.sources import FEEDS
from app.scanners.scanner import Scanner

pytestmark = pytest.mark.live
REAL = load_secrets()
UA = "ATLAS-Trading-AI/0.4 (research test)"


@pytest.mark.parametrize("name", ["fed_monetary", "sec_press", "cointelegraph", "kraken_status", "marketwatch_pulse"])
def test_real_feed_parses(name):
    src = next(f for f in FEEDS if f.name == name)
    items = fetch_feed(src.url, name, requests.Session(), UA)
    assert items and all(i.title for i in items)
    assert sum(i.published is not None for i in items) >= len(items) * 0.8


def test_real_sec_submissions_and_form4(tmp_path):
    if not REAL.contact_email:
        pytest.skip("ATLAS_CONTACT_EMAIL not set (SEC requires a contact User-Agent)")
    sec = SecClient(requests.Session(), REAL.contact_email, tmp_path)
    assert sec.company_name("NVDA").upper().startswith("NVIDIA")
    fs = sec.recent_filings("NVDA", datetime.now(timezone.utc) - timedelta(days=60))
    assert fs and all(f.accepted.tzinfo for f in fs)
    f4 = [f for f in fs if f.form == "4"]
    if f4:
        txs = sec.form4_transactions(f4[0])
        assert all(t["code"] for t in txs)


def test_real_full_ingest_and_context(settings):
    ctx = bootstrap(settings, REAL, console_log=False)
    try:
        ne = NewsEngine(ctx.settings, ctx.db, ctx.secrets, ctx.safe_state)
        st = ne.ingest()
        assert st["ok"] >= 10, st["sources"]
        assert st["new"] >= 100
        assert ne.health()["healthy"]
        assert ctx.safe_state.trading_allowed()
        assert st["sources"]["x_api_v2"]["status"] in ("needs_credentials", "ok")
        for sym, ac in (("NVDA", "stock"), ("BTC-USD", "crypto")):
            c = ne.symbol_context(sym, ac)
            assert c["health"]["healthy"]
            assert c["aggregate"] is None or -1 <= c["aggregate"] <= 1
        n = ctx.db.conn.execute("SELECT COUNT(*) FROM news_events WHERE published_ts IS NOT NULL").fetchone()[0]
        assert n >= 100
        assert ctx.db.conn.execute("SELECT COUNT(*) FROM news_events WHERE verified=1").fetchone()[0] > 0
    finally:
        ctx.close()


def test_real_crypto_scan_with_news(settings):
    ctx = bootstrap(settings, REAL, console_log=False)
    try:
        ne = NewsEngine(ctx.settings, ctx.db, ctx.secrets, ctx.safe_state)
        sc = Scanner(ctx.settings, MarketDataEngine(ctx.settings, ctx.db, ctx.safe_state), ne)
        rep = sc.run(AssetClass.CRYPTO, ["BTC-USD", "ETH-USD"], "live")
        assert rep["news"]["ingested"] and rep["news"]["health"]["healthy"]
        for r in rep["symbols"]:
            if "skipped" in r:
                continue
            assert r["news"]["healthy"] and r["sentiment"]["composite"] is not None
            for s in r["signals"]:
                row = ctx.db.conn.execute("SELECT news FROM signal_scores WHERE signal_id=?",
                                          (s["signal_id"],)).fetchone()
                assert row["news"] is not None
    finally:
        ctx.close()
