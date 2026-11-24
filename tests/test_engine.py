from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.core.safety import SafeState
from app.data.engine import MarketDataEngine
from app.data.models import AssetClass, DataStatus
from app.data.providers.base import DataFormatError, ProviderError, SymbolNotSupported
from tests.fakes import FakeProvider, bars

END = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)
NOW = END + timedelta(seconds=30)


def make(settings, db, **provs):
    base = {n: FakeProvider(n, bars("BTC-USD"), price=100.0, bid=99.99, ask=100.01)
            for n in ("coinbase", "kraken", "binance")}
    base["yahoo"] = FakeProvider("yahoo", bars("AAPL", ac=AssetClass.STOCK, session="regular"), price=50.0)
    base.update(provs)
    safe = SafeState(db)
    return MarketDataEngine(settings, db, safe, providers=base), safe


def count(db, sql, *a):
    return db.conn.execute(sql, a).fetchone()[0]


def test_happy_path_live_crosschecked_and_persisted(settings, db):
    eng, safe = make(settings, db)
    r = eng.get_candles("BTC-USD", "5m", now=NOW)
    assert r.series.status == DataStatus.LIVE and r.series.tradable
    assert r.crosscheck.verdict == "agree" and safe.trading_allowed()
    assert count(db, "SELECT COUNT(*) FROM market_observations WHERE kind='candle'") == 30  # 3 sources x 10
    assert count(db, "SELECT COUNT(*) FROM crosscheck_results") == 1


def test_fallback_when_primary_unreachable_no_halt(settings, db):
    eng, safe = make(settings, db, coinbase=FakeProvider("coinbase", exc=ProviderError("timeout")))
    r = eng.get_candles("BTC-USD", "5m", now=NOW)
    assert r.series.source == "kraken" and r.series.tradable
    assert safe.trading_allowed()
    assert any("sources failed" in w for w in r.series.warnings)
    assert count(db, "SELECT COUNT(*) FROM system_events WHERE event_type='source_unreachable'") == 1


def test_invalid_ohlc_triggers_safe_state(settings, db):
    bad = bars("BTC-USD")
    bad[2] = replace(bad[2], high=bad[2].low / 2)
    eng, safe = make(settings, db, coinbase=FakeProvider("coinbase", bad))
    r = eng.get_candles("BTC-USD", "5m", now=NOW)
    assert not safe.trading_allowed() and "invalid candles from coinbase" in safe.status().reason
    assert r.series.source == "kraken"   # valid data still returned, but trading is halted
    assert count(db, "SELECT COUNT(*) FROM market_observations WHERE source='coinbase'") == 0


def test_malformed_payload_triggers_safe_state_and_error_row(settings, db):
    eng, safe = make(settings, db, kraken=FakeProvider("kraken", exc=DataFormatError("schema changed")))
    eng.get_candles("BTC-USD", "5m", now=NOW)
    assert not safe.trading_allowed()
    assert count(db, "SELECT COUNT(*) FROM errors WHERE module='data.kraken'") == 1


def test_all_sources_down_returns_cached_and_halts(settings, db):
    eng, safe = make(settings, db)
    eng.get_candles("BTC-USD", "5m", now=NOW)               # warm the cache
    down = {n: FakeProvider(n, exc=ProviderError("down")) for n in ("coinbase", "kraken", "binance")}
    eng2, safe2 = make(settings, db, **down)
    r = eng2.get_candles("BTC-USD", "5m", now=NOW)
    assert r.series.status == DataStatus.CACHED and not r.series.tradable
    assert len(r.series.candles) == 9 and {c.source for c in r.series.candles} == {"coinbase"}
    assert r.halted and not safe2.trading_allowed()


def test_nothing_cached_is_unavailable(settings, db):
    down = {n: FakeProvider(n, exc=ProviderError("down")) for n in ("coinbase", "kraken", "binance")}
    eng, safe = make(settings, db, **down)
    r = eng.get_candles("ETH-USD", "5m", now=NOW)
    assert r.series.status == DataStatus.UNAVAILABLE and not safe.trading_allowed()


def test_unsupported_symbol_does_not_halt(settings, db):
    eng, safe = make(settings, db, yahoo=FakeProvider("yahoo", exc=SymbolNotSupported("404")))
    r = eng.get_candles("ZZZZ", "5m", now=NOW)
    assert r.series.status == DataStatus.UNAVAILABLE and safe.trading_allowed()


def test_divergent_sources_halt(settings, db):
    hi = [replace(c, open=c.open * 1.1, high=c.high * 1.1, low=c.low * 1.1, close=c.close * 1.1)
          for c in bars("BTC-USD")]
    eng, safe = make(settings, db, kraken=FakeProvider("kraken", hi),
                     binance=FakeProvider("binance", exc=ProviderError("down")))
    r = eng.get_candles("BTC-USD", "5m", now=NOW)
    assert r.crosscheck.verdict == "divergent" and not safe.trading_allowed()


def test_outlier_primary_is_replaced(settings, db):
    hi = [replace(c, open=c.open * 1.02, high=c.high * 1.02, low=c.low * 1.02, close=c.close * 1.02)
          for c in bars("BTC-USD")]
    eng, safe = make(settings, db, coinbase=FakeProvider("coinbase", hi))
    r = eng.get_candles("BTC-USD", "5m", now=NOW)
    assert r.crosscheck.verdict == "outlier_excluded" and r.series.source == "kraken"
    assert safe.trading_allowed()


def test_forming_bar_updated_but_final_bar_never_rewritten(settings, db):
    eng, _ = make(settings, db, kraken=FakeProvider("kraken", exc=ProviderError("x")),
                  binance=FakeProvider("binance", exc=ProviderError("x")))
    eng.get_candles("BTC-USD", "5m", now=NOW)
    cs = bars("BTC-USD")
    last = cs[-1]
    cs[-1] = replace(last, close=last.close * 1.0003, high=last.high * 1.0003, is_complete=True)
    cs[0] = replace(cs[0], close=cs[0].close * 1.0001, high=cs[0].high * 1.0001)  # revision of a final bar
    eng.providers["coinbase"] = FakeProvider("coinbase", cs)
    eng.get_candles("BTC-USD", "5m", now=END + timedelta(minutes=5, seconds=5))
    rows = db.conn.execute("SELECT source_ts, close, is_complete_candle FROM market_observations "
                           "WHERE kind='candle' ORDER BY source_ts").fetchall()
    assert len(rows) == 10
    assert rows[-1]["is_complete_candle"] == 1 and rows[-1]["close"] == pytest.approx(cs[-1].close)
    assert rows[0]["close"] == pytest.approx(bars("BTC-USD")[0].close)   # original kept


def test_quote_consensus_persists_and_halts_on_crossed_book(settings, db):
    eng, safe = make(settings, db)
    cc, quotes, _ = eng.get_quote_consensus("BTC-USD")
    assert cc.verdict == "agree" and len(quotes) == 3
    assert count(db, "SELECT COUNT(*) FROM market_observations WHERE kind='quote'") == 3
    eng2, safe2 = make(settings, db, kraken=FakeProvider("kraken", price=100, bid=101, ask=99))
    eng2.get_quote_consensus("BTC-USD")
    assert not safe2.trading_allowed() and "crossed" in safe2.status().reason


def test_quote_divergence_halts(settings, db):
    eng, safe = make(settings, db, kraken=FakeProvider("kraken", price=120),
                     binance=FakeProvider("binance", price=140))
    cc, _, _ = eng.get_quote_consensus("BTC-USD")
    assert cc.verdict == "divergent" and not safe.trading_allowed()


def test_stock_single_source(settings, db):
    eng, safe = make(settings, db)
    cc, _, _ = eng.get_quote_consensus("AAPL")
    assert cc.verdict == "single_source" and safe.trading_allowed()


def test_collect_survives_unexpected_exception(settings, db):
    eng, safe = make(settings, db, yahoo=FakeProvider("yahoo", exc=ZeroDivisionError("bug")))
    rep = eng.collect(["AAPL", "BTC-USD"])
    assert rep[0]["status"] == "error" and rep[1]["symbol"] == "BTC-USD"
    assert not safe.trading_allowed()
    assert count(db, "SELECT COUNT(*) FROM errors WHERE module='data.engine'") == 1
