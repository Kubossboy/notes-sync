from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.config import load_settings
from app.data.calendar import CalendarUnknown, is_trading_day, session_at
from app.data.crosscheck import cross_check
from app.data.models import AssetClass, CandleSeries, DataStatus
from app.data.validation import label_freshness, validate_candles
from tests.fakes import bars

CFG = load_settings().data
END = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)


def _series(cs, ac=AssetClass.CRYPTO, tf="5m"):
    return CandleSeries(cs[0].symbol if cs else "X", ac, "fake", tf, cs, END)


def test_clean_series_has_no_issues():
    assert validate_candles(bars("BTC-USD"), "5m", AssetClass.CRYPTO, CFG, END) == ([], [])


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: replace(c, high=c.low * 0.5), "OHLC inconsistent"),
    (lambda c: replace(c, low=c.high * 2, high=c.high * 2.1), "OHLC inconsistent"),
    (lambda c: replace(c, open=-1), "non-positive"),
    (lambda c: replace(c, volume=-5), "negative volume"),
    (lambda c: replace(c, open_time=c.open_time + timedelta(seconds=7)), "aligned"),
    (lambda c: replace(c, timeframe="1m"), "timeframe mismatch"),
])
def test_integrity_violations(mutate, needle):
    cs = bars("BTC-USD")
    cs[3] = mutate(cs[3])
    issues, _ = validate_candles(cs, "5m", AssetClass.CRYPTO, CFG, END)
    assert any(needle in i for i in issues), issues


def test_duplicates_future_and_misplaced_forming_bar():
    cs = bars("BTC-USD")
    dup = cs[:5] + [cs[4]] + cs[5:]
    assert any("duplicate" in i for i in validate_candles(dup, "5m", AssetClass.CRYPTO, CFG, END)[0])
    fut = bars("BTC-USD", end=END + timedelta(hours=1))
    assert any("future" in i for i in validate_candles(fut, "5m", AssetClass.CRYPTO, CFG, END)[0])
    cs2 = bars("BTC-USD", forming=False)
    cs2[2] = replace(cs2[2], is_complete=False)
    assert any("incomplete candle is not the last" in i
               for i in validate_candles(cs2, "5m", AssetClass.CRYPTO, CFG, END)[0])
    assert validate_candles([], "5m", AssetClass.CRYPTO, CFG, END)[0] == ["no candles returned"]


def test_gap_and_spike_are_warnings():
    cs = bars("BTC-USD")
    del cs[4]
    cs[6] = replace(cs[6], close=cs[6].open * 1.5, high=cs[6].open * 1.5)
    issues, warns = validate_candles(cs, "5m", AssetClass.CRYPTO, CFG, END)
    assert issues == []
    assert any("missing bar" in w for w in warns) and any("spike" in w for w in warns)


def test_stock_overnight_gap_not_counted():
    a = bars("AAPL", 3, end=datetime(2026, 11, 23, 20, 50, tzinfo=timezone.utc), ac=AssetClass.STOCK,
             forming=False, session="regular")
    b = bars("AAPL", 3, end=datetime(2026, 11, 24, 14, 40, tzinfo=timezone.utc), ac=AssetClass.STOCK,
             forming=False, session="regular", start_price=a[-1].close)
    _, warns = validate_candles(a + b, "5m", AssetClass.STOCK, CFG, datetime(2026, 11, 24, 15, tzinfo=timezone.utc))
    assert not any("missing bar" in w for w in warns)


def test_crypto_freshness_live_delayed_stale():
    cs = bars("BTC-USD")  # last complete bar opens 07:55, closes 08:00
    s = label_freshness(_series(cs), CFG, END + timedelta(seconds=30))
    assert s.status == DataStatus.LIVE and not s.stale and s.tradable
    s = label_freshness(_series(cs), CFG, END + timedelta(minutes=15))
    assert s.status == DataStatus.DELAYED and not s.stale and not s.tradable
    s = label_freshness(_series(cs), CFG, END + timedelta(minutes=40))
    assert s.status == DataStatus.DELAYED and s.stale


def test_cached_and_unavailable_never_tradable():
    s = label_freshness(_series(bars("BTC-USD")), CFG, END + timedelta(seconds=30), from_cache=True)
    assert s.status == DataStatus.CACHED and not s.tradable
    assert label_freshness(_series([]), CFG, END).status == DataStatus.UNAVAILABLE
    only_forming = bars("BTC-USD", 1)
    assert label_freshness(_series(only_forming), CFG, END).status == DataStatus.UNAVAILABLE


def test_stock_market_closed_is_delayed_not_stale():
    cs = bars("AAPL", 5, end=datetime(2026, 11, 23, 20, 55, tzinfo=timezone.utc), ac=AssetClass.STOCK,
              forming=False, session="regular")
    s = label_freshness(_series(cs, AssetClass.STOCK), CFG, datetime(2026, 11, 24, 8, tzinfo=timezone.utc))
    assert s.status == DataStatus.DELAYED and s.market_open is False and not s.stale
    open_now = datetime(2026, 11, 23, 21, 1, tzinfo=timezone.utc)  # 16:01 ET -> post-market
    s = label_freshness(_series(cs, AssetClass.STOCK), CFG, open_now)
    assert s.market_open and s.status == DataStatus.LIVE


def test_series_with_issues_not_tradable():
    s = label_freshness(_series(bars("BTC-USD")), CFG, END + timedelta(seconds=30))
    s.issues.append("x")
    assert not s.tradable


def test_calendar():
    assert not is_trading_day(datetime(2026, 11, 26).date())       # Thanksgiving
    assert not is_trading_day(datetime(2026, 11, 28).date())       # Saturday
    assert is_trading_day(datetime(2026, 11, 24).date())
    et_open = datetime(2026, 11, 24, 14, 30, tzinfo=timezone.utc)  # EST: 09:30
    assert session_at(et_open) == "regular"
    assert session_at(et_open - timedelta(minutes=1)) == "pre"
    assert session_at(datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)) == "regular"  # EDT 09:30
    assert session_at(datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc)) == "post"   # early close 13:00 ET
    assert session_at(datetime(2026, 11, 24, 2, 0, tzinfo=timezone.utc)) == "closed"
    with pytest.raises(CalendarUnknown):
        is_trading_day(datetime(2031, 1, 2).date())


def test_crosscheck_verdicts():
    ac = AssetClass.CRYPTO
    assert cross_check("B", ac, {"a": 100, "b": 100.1, "c": 100.05}, CFG).verdict == "agree"
    cc = cross_check("B", ac, {"a": 100, "b": 100.1, "c": 106}, CFG)
    assert cc.verdict == "outlier_excluded" and cc.outliers == ["c"] and cc.consensus == pytest.approx(100.05)
    assert cross_check("B", ac, {"a": 100, "b": 110}, CFG).verdict == "divergent"
    assert cross_check("B", ac, {"a": 100}, CFG).verdict == "single_source"
    assert cross_check("B", ac, {"a": None, "b": 0}, CFG).verdict == "unavailable"
    assert not cross_check("B", ac, {"a": 100}, CFG).reliable
