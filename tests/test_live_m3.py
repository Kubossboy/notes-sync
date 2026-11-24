"""Milestone 3 live tests on real market data (pytest -m live)."""
import pytest

from app.config import Secrets, load_secrets
from app.core.bootstrap import bootstrap
from app.data.engine import MarketDataEngine
from app.data.models import AssetClass
from app.data.orderbook import fetch_book
from app.data.providers.crypto import BinanceProvider, CoinbaseProvider, KrakenProvider
from app.integrations.registry import INTEGRATIONS, probe
from app.scanners.scanner import Scanner
from app.technical.indicators import compute_all, support_resistance, to_frame

pytestmark = pytest.mark.live
REAL = load_secrets()
HAS_ALPACA = REAL.has("alpaca_api_key_id") and REAL.has("alpaca_api_secret_key")


def test_indicators_on_real_btc():
    c = CoinbaseProvider().fetch_candles("BTC-USD", "15m", 300)
    df = compute_all(to_frame(c), AssetClass.CRYPTO)
    last = df.iloc[-1]
    assert 0 <= last["rsi14"] <= 100
    assert last["atr14"] > 0 and last["bb_lower"] < last["bb_mid"] < last["bb_upper"]
    assert df["low"].min() <= last["vwap"] <= df["high"].max()
    assert last["sma200"] > 0 and last["trend"] in ("up", "down", "range")
    assert isinstance(support_resistance(df), list)


@pytest.mark.parametrize("prov", [CoinbaseProvider, KrakenProvider, BinanceProvider])
def test_real_order_books(prov):
    b = fetch_book(prov(), "BTC-USD")
    assert b.best_ask > b.best_bid > 0 and b.spread_bps < 20
    bid, ask = b.depth_usd(0.5)
    assert bid > 0 and ask > 0 and -1 <= b.imbalance(0.5) <= 1


def test_live_crypto_scan(settings):
    ctx = bootstrap(settings, Secrets(), console_log=False)
    try:
        sc = Scanner(ctx.settings, MarketDataEngine(ctx.settings, ctx.db, ctx.safe_state))
        rep = sc.run(AssetClass.CRYPTO, ["BTC-USD", "ETH-USD"], "live")
        assert ctx.safe_state.trading_allowed(), ctx.safe_state.status()
        for r in rep["symbols"]:
            assert "skipped" not in r, r
            assert len(r["no_setup"]) + len(r["signals"]) == 7
            assert not any(v.startswith("error") for v in r["no_setup"].values()), r["no_setup"]
            for s in r["signals"]:
                assert s["status"] in ("rejected", "incomplete") and s["persisted"]
        assert ctx.db.conn.execute("SELECT COUNT(*) FROM orderbook_snapshots").fetchone()[0] == 2
    finally:
        ctx.close()


def test_research_stock_scan(settings):
    ctx = bootstrap(settings, Secrets(), console_log=False)
    try:
        sc = Scanner(ctx.settings, MarketDataEngine(ctx.settings, ctx.db, ctx.safe_state))
        rep = sc.run(AssetClass.STOCK, ["SPY", "AAPL"], "research")
        for r in rep["symbols"]:
            assert "skipped" not in r, r
            assert len(r["no_setup"]) + len(r["signals"]) == 7
            assert not any(v.startswith("error") for v in r["no_setup"].values()), r["no_setup"]
        assert ctx.db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    finally:
        ctx.close()


@pytest.mark.skipif(not HAS_ALPACA, reason="ALPACA_* keys not set (needs_credentials)")
def test_alpaca_live_data_and_paper_account(settings):
    for name in ("alpaca_paper", "alpaca_market_data"):
        r = probe(next(i for i in INTEGRATIONS if i.name == name), settings, REAL)
        assert r.status == "ok", (name, r.detail)
    from app.data.providers.alpaca import AlpacaDataProvider
    from app.data.validation import validate_candles
    p = AlpacaDataProvider(REAL)
    c = p.fetch_candles("SPY", "5m", 100)
    assert len(c) >= 20 and validate_candles(c, "5m", AssetClass.STOCK, settings.data)[0] == []


@pytest.mark.skipif(not HAS_ALPACA, reason="ALPACA_* keys not set")
def test_yahoo_alpaca_cross_check(settings):
    ctx = bootstrap(settings, REAL, console_log=False)
    try:
        eng = MarketDataEngine(ctx.settings, ctx.db, ctx.safe_state, secrets=REAL)
        r = eng.get_candles("SPY", "5m", 300)
        assert r.crosscheck is not None and r.crosscheck.verdict == "agree", r.crosscheck
        assert ctx.safe_state.trading_allowed()
    finally:
        ctx.close()
