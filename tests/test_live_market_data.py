"""Real network tests against free public APIs (run with: pytest -m live)."""
from datetime import datetime, timezone

import pytest

from app.config import Secrets
from app.core.bootstrap import bootstrap
from app.data.engine import MarketDataEngine
from app.data.models import DataStatus
from app.data.providers.crypto import BinanceProvider, CoinbaseProvider, KrakenProvider
from app.data.providers.yahoo import YahooProvider
from app.data.validation import validate_candles

pytestmark = pytest.mark.live


@pytest.mark.parametrize("prov,symbol", [(CoinbaseProvider, "BTC-USD"), (KrakenProvider, "BTC-USD"),
                                         (BinanceProvider, "ETH-USD"), (YahooProvider, "AAPL")])
@pytest.mark.parametrize("tf", ["1m", "5m", "1h"])
def test_provider_returns_valid_real_candles(settings, prov, symbol, tf):
    p = prov()
    c = p.fetch_candles(symbol, tf, 60)
    assert len(c) >= 10
    issues, _ = validate_candles(c, tf, p.asset_class, settings.data)
    assert issues == [], issues
    assert all(x.is_complete for x in c[:-1])
    assert c[-1].open_time <= datetime.now(timezone.utc)
    q = p.fetch_quote(symbol)
    assert q.price > 0 and abs(q.price / c[-1].close - 1) < 0.05


def test_engine_live_collect(settings):
    ctx = bootstrap(settings, Secrets(), console_log=False)
    try:
        eng = MarketDataEngine(ctx.settings, ctx.db, ctx.safe_state)
        rep = {r["symbol"]: r for r in eng.collect(["SPY", "BTC-USD", "ETH-USD"], "5m", 50)}
        assert ctx.safe_state.trading_allowed(), ctx.safe_state.status()
        for s in ("BTC-USD", "ETH-USD"):
            assert rep[s]["status"] == DataStatus.LIVE.value and rep[s]["tradable"]
            assert rep[s]["quote_check"] == "agree" and rep[s]["candle_check"] in ("agree", "outlier_excluded")
        assert rep["SPY"]["status"] in ("live", "delayed") and rep["SPY"]["bars"] > 0
        n = ctx.db.conn.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0]
        assert n >= 100
    finally:
        ctx.close()
