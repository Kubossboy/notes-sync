from datetime import datetime, timezone

import pytest
import requests

from app.core.safety import LiveTradingForbidden
from app.data.providers.base import DataFormatError, ProviderError, SymbolNotSupported
from app.data.providers.crypto import BinanceProvider, CoinbaseProvider, KrakenProvider, parse_iso_utc
from app.data.providers.yahoo import YahooProvider
from tests.fakes import FakeResp, SeqSession

NOW = datetime(2026, 11, 24, 8, 12, 0, tzinfo=timezone.utc)
T0 = int(datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc).timestamp())


def test_symbol_mapping():
    assert CoinbaseProvider().map_symbol("btc-usd") == "BTC-USD"
    assert KrakenProvider().map_symbol("BTC-USD") == "XBTUSD"
    assert KrakenProvider().map_symbol("ETH-USD") == "ETHUSD"
    assert BinanceProvider().map_symbol("SOL-USD") == "SOLUSDT"
    assert YahooProvider().map_symbol("brk.b") == "BRK-B"
    for bad in ("BTCUSD", "BTC-EUR"):
        with pytest.raises(SymbolNotSupported):
            CoinbaseProvider().map_symbol(bad)
    with pytest.raises(SymbolNotSupported):
        YahooProvider().map_symbol("BTC-USD")


def test_coinbase_parses_newest_first_and_flags_forming_bar():
    rows = [[T0 + 600, 99, 101, 100, 100.5, 3], [T0 + 300, 98, 100, 99, 99.5, 2], [T0, 97, 99, 98, 98.5, 1]]
    p = CoinbaseProvider(session=SeqSession(FakeResp(200, rows)))
    c = p.fetch_candles("BTC-USD", "5m", now=NOW)
    assert [x.open_time.timestamp() for x in c] == [T0, T0 + 300, T0 + 600]
    assert (c[0].open, c[0].high, c[0].low, c[0].close, c[0].volume) == (98, 99, 97, 98.5, 1)
    assert [x.is_complete for x in c] == [True, True, False]   # 08:10 bar closes 08:15 > NOW


def test_coinbase_quote_nanosecond_timestamp():
    p = CoinbaseProvider(session=SeqSession(FakeResp(200, {
        "price": "100.5", "bid": "100.4", "ask": "100.6", "time": "2026-11-24T08:10:55.998464629Z"})))
    q = p.fetch_quote("BTC-USD")
    assert q.price == 100.5 and q.source_ts == parse_iso_utc("2026-11-24T08:10:55.998464Z")
    assert round(q.spread_bps, 2) == 19.9


def test_kraken_parses_and_last_bar_forming():
    rows = [[T0, "1", "2", "0.5", "1.5", "1.2", "10", 5], [T0 + 600, "1.5", "2", "1", "1.8", "1.6", "4", 2]]
    p = KrakenProvider(session=SeqSession(FakeResp(200, {"error": [], "result": {"XXBTZUSD": rows, "last": 1}})))
    c = p.fetch_candles("BTC-USD", "5m", now=NOW)
    assert c[0].close == 1.5 and c[0].volume == 10 and c[0].is_complete and not c[1].is_complete


def test_kraken_unknown_pair_and_error():
    p = KrakenProvider(session=SeqSession(FakeResp(200, {"error": ["EQuery:Unknown asset pair"]})))
    with pytest.raises(SymbolNotSupported):
        p.fetch_candles("ZZZ-USD", "5m")
    p = KrakenProvider(session=SeqSession(FakeResp(200, {"error": ["EService:Unavailable"]})))
    with pytest.raises(DataFormatError):
        p.fetch_candles("BTC-USD", "5m")


def test_binance_uses_close_time_and_usdt_tag():
    rows = [[T0 * 1000, "1", "2", "0.5", "1.5", "7", (T0 + 300) * 1000 - 1, "0", 1, "0", "0", "0"]]
    p = BinanceProvider(session=SeqSession(FakeResp(200, rows),
                                           FakeResp(200, {"bidPrice": "10", "askPrice": "10.02"})))
    c = p.fetch_candles("BTC-USD", "5m", now=NOW)
    assert c[0].is_complete and c[0].volume == 7
    q = p.fetch_quote("BTC-USD")
    assert q.quote_currency == "USDT" and q.price == pytest.approx(10.01)


def test_yahoo_skips_nulls_marks_partial_bar_and_sessions():
    # 14:30 UTC = 09:30 ET regular session; last ts is off-grid (Yahoo's live partial bar)
    t = int(datetime(2026, 11, 23, 14, 30, tzinfo=timezone.utc).timestamp())
    ts = [t - 300, t, t + 300, t + 437]
    payload = {"chart": {"result": [{"meta": {"regularMarketPrice": 5, "regularMarketTime": t},
                                     "timestamp": ts, "indicators": {"quote": [{
        "open": [1, 1, None, 1.2], "high": [1.1, 1.3, None, 1.25], "low": [0.9, 0.95, None, 1.15],
        "close": [1.05, 1.2, None, 1.2], "volume": [10, None, None, 3]}]}}], "error": None}}
    p = YahooProvider(session=SeqSession(FakeResp(200, payload)))
    now = datetime(2026, 11, 23, 14, 38, tzinfo=timezone.utc)
    c = p.fetch_candles("AAPL", "5m", now=now)
    assert len(c) == 3 and p.last_skipped_nulls == 1
    assert [x.session for x in c] == ["pre", "regular", "regular"]
    assert [x.is_complete for x in c] == [True, True, False]
    assert c[1].volume == 0


def test_yahoo_errors():
    p = YahooProvider(session=SeqSession(FakeResp(200, {"chart": {"result": None,
                                                              "error": {"code": "Not Found"}}})))
    with pytest.raises(SymbolNotSupported):
        p.fetch_candles("ZZZZ", "5m")
    p = YahooProvider(session=SeqSession(FakeResp(200, {"chart": {"result": [{"timestamp": [1],
                                                                             "indicators": {}}]}})))
    with pytest.raises(DataFormatError):
        p.fetch_candles("AAPL", "5m")


def test_non_numeric_value_is_format_error():
    rows = [[T0, 97, "abc", 98, 98.5, 1]]
    with pytest.raises(DataFormatError):
        CoinbaseProvider(session=SeqSession(FakeResp(200, rows))).fetch_candles("BTC-USD", "5m", now=NOW)


def test_retry_then_success_and_404_unsupported():
    rows = [[T0, 97, 99, 98, 98.5, 1]]
    s = SeqSession(FakeResp(429, {}), requests.ConnectionError("x"), FakeResp(200, rows))
    p = CoinbaseProvider(session=s); p.backoff_s = 0; p.min_interval_s = 0
    assert len(p.fetch_candles("BTC-USD", "5m", now=NOW)) == 1 and len(s.calls) == 3
    p = CoinbaseProvider(session=SeqSession(FakeResp(404, {"message": "NotFound"})))
    with pytest.raises(SymbolNotSupported):
        p.fetch_candles("ZZZ-USD", "5m")


def test_retries_exhausted_raises_provider_error():
    s = SeqSession(FakeResp(503, {}), FakeResp(503, {}), FakeResp(503, {}))
    p = CoinbaseProvider(session=s); p.backoff_s = 0; p.min_interval_s = 0
    with pytest.raises(ProviderError):
        p.fetch_candles("BTC-USD", "5m")
    assert len(s.calls) == 3


def test_non_json_is_format_error():
    p = CoinbaseProvider(session=SeqSession(FakeResp(200, None, text="<html>")))
    with pytest.raises(DataFormatError):
        p.fetch_candles("BTC-USD", "5m")


def test_live_endpoint_guard_applies_to_providers():
    p = CoinbaseProvider(session=SeqSession())
    with pytest.raises(LiveTradingForbidden):
        p._get_json("https://api.alpaca.markets/v2/orders")


def test_unsupported_timeframe():
    with pytest.raises(SymbolNotSupported):
        CoinbaseProvider().fetch_candles("BTC-USD", "3m")


def test_yahoo_hourly_session_anchored_bars():
    base = int(datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc).timestamp())
    ts = [base, base + 3600, base + 5400, base + 9000]   # 13:00, 14:00 (30-min bar), 14:30, 15:30
    n = len(ts)
    payload = {"chart": {"result": [{"meta": {}, "timestamp": ts, "indicators": {"quote": [{
        "open": [1] * n, "high": [1.1] * n, "low": [0.9] * n, "close": [1] * n, "volume": [1] * n}]}}]}}
    p = YahooProvider(session=SeqSession(FakeResp(200, payload)))
    now = datetime(2026, 11, 23, 15, 45, tzinfo=timezone.utc)
    c = p.fetch_candles("AAPL", "1h", now=now)
    assert [x.is_complete for x in c] == [True, True, True, False]
    from app.config import load_settings
    from app.data.validation import validate_candles
    assert validate_candles(c, "1h", c[0].asset_class, load_settings().data, now)[0] == []
