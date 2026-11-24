import pytest

from app.core.safety import LiveTradingForbidden, SafeState, assert_paper_only, assert_url_allowed
from app.database.db import Database


@pytest.mark.parametrize("url", [
    "https://api.alpaca.markets/v2/orders",
    "https://API.ALPACA.MARKETS/v2/account",
    "https://api.coinbase.com/api/v3/brokerage/orders",
    "https://api.binance.com/api/v3/order",
    "https://api.kraken.com/0/private/AddOrder",
])
def test_live_endpoints_blocked(url):
    with pytest.raises(LiveTradingForbidden):
        assert_url_allowed(url)


@pytest.mark.parametrize("url", [
    "https://paper-api.alpaca.markets/v2/orders",
    "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT",
    "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
    "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
])
def test_paper_and_public_endpoints_allowed(url):
    assert_url_allowed(url)


def test_paper_only_assertion_passes_on_defaults(settings):
    assert_paper_only(settings)


def test_safe_state_halt_resume_persists(tmp_path):
    p = tmp_path / "s.sqlite3"
    d = Database(p); d.migrate()
    s = SafeState(d)
    assert s.trading_allowed()
    s.halt("missing market data")
    s.halt("missing market data")  # idempotent, not double-logged
    d.close()
    d2 = Database(p)
    s2 = SafeState(d2)
    assert not s2.trading_allowed() and s2.status().reason == "missing market data"
    n = d2.conn.execute("SELECT COUNT(*) FROM system_events WHERE event_type='safe_state_entered'").fetchone()[0]
    assert n == 1
    s2.resume("test")
    assert s2.trading_allowed()
    d2.close()
