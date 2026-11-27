import pytest

from app.config import Secrets
from app.core.safety import SafeState
from app.data.engine import MarketDataEngine
from app.data.providers.alpaca import AlpacaCredentialsMissing, AlpacaDataProvider
from app.data.providers.base import DataFormatError, EmptyBook
from tests.fakes import FakeResp, SeqSession

SEC = Secrets(alpaca_api_key_id="PKTEST", alpaca_api_secret_key="SKTEST")


def test_requires_credentials():
    with pytest.raises(AlpacaCredentialsMissing):
        AlpacaDataProvider(Secrets())


def test_engine_disables_alpaca_without_keys(settings, db):
    eng = MarketDataEngine(settings, db, SafeState(db), secrets=Secrets())
    assert "alpaca" not in eng.providers and "needs_credentials" in eng.disabled["alpaca"]


def test_engine_enables_alpaca_with_keys(settings, db):
    eng = MarketDataEngine(settings, db, SafeState(db), secrets=SEC)
    assert "alpaca" in eng.providers


def test_bars_parsed_with_auth_headers_and_data_host():
    from datetime import datetime, timezone
    payload = {"bars": [{"t": "2026-11-23T15:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100},
                        {"t": "2026-11-23T15:05:00Z", "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 50}],
               "symbol": "AAPL", "next_page_token": None}
    sess = SeqSession(FakeResp(200, payload))
    sess_calls = []
    orig = sess.get
    def get(url, params=None, timeout=None, headers=None):
        sess_calls.append(headers)
        return orig(url, params=params, timeout=timeout, headers=headers)
    sess.get = get
    p = AlpacaDataProvider(SEC, session=sess)
    c = p.fetch_candles("AAPL", "5m", now=datetime(2026, 11, 23, 15, 8, tzinfo=timezone.utc))
    assert [x.is_complete for x in c] == [True, False] and c[0].session == "regular"
    url, params = sess.calls[0]
    assert url == "https://data.alpaca.markets/v2/stocks/AAPL/bars" and params["feed"] == "iex"
    assert sess_calls[0]["APCA-API-KEY-ID"] == "PKTEST"


def test_quote_and_bad_payloads():
    p = AlpacaDataProvider(SEC, session=SeqSession(FakeResp(200, {"quote": {"bp": 10, "ap": 10.02,
                                                                            "t": "2026-11-23T15:00:00.1Z"}})))
    q = p.fetch_quote("AAPL")
    assert q.price == pytest.approx(10.01) and q.source_ts is not None
    p = AlpacaDataProvider(SEC, session=SeqSession(FakeResp(200, {"quote": {"bp": 0, "ap": 0}})))
    with pytest.raises(EmptyBook):          # D99: empty book = no quote from this source, not bad data
        p.fetch_quote("AAPL")
    p = AlpacaDataProvider(SEC, session=SeqSession(FakeResp(200, {"oops": 1})))
    with pytest.raises(DataFormatError):
        p.fetch_candles("AAPL", "5m")


def test_secret_not_in_repr():
    p = AlpacaDataProvider(SEC)
    assert "SKTEST" not in repr(p.__dict__.get("_secret"))
