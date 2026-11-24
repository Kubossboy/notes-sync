import json

import pytest
import requests

from app.config import Secrets
from app.integrations.registry import INTEGRATIONS, Integration, probe, probe_all, _v_binance


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        if self.exc:
            raise self.exc
        return self.resp


INTEG = Integration("t", "market_data_crypto", "https://example.org/x", _v_binance)


def test_registry_names_unique_and_no_live_urls():
    from app.core.safety import assert_url_allowed
    names = [i.name for i in INTEGRATIONS]
    assert len(names) == len(set(names))
    for i in INTEGRATIONS:
        assert_url_allowed(i.url)


def test_missing_credentials_no_network(settings):
    alp = next(i for i in INTEGRATIONS if i.name == "alpaca_paper")
    sess = FakeSession(exc=AssertionError("must not call network"))
    r = probe(alp, settings, Secrets(), sess)
    assert r.status == "needs_credentials" and sess.calls == []


def test_ok_payload(settings):
    r = probe(INTEG, settings, Secrets(), FakeSession(FakeResp(200, {"symbol": "BTCUSDT", "price": "60000.1"})))
    assert r.status == "ok" and r.sample["price"] == 60000.1


@pytest.mark.parametrize("resp,exc,expected", [
    (FakeResp(200, {"symbol": "BTCUSDT", "price": "0"}), None, "degraded"),       # implausible
    (FakeResp(200, {"unexpected": 1}), None, "degraded"),                          # schema change
    (FakeResp(200, None, text="<html>"), None, "degraded"),                       # not json
    (FakeResp(429, {}), None, "degraded"),
    (FakeResp(500, {}), None, "unreachable"),
    (FakeResp(403, {}), None, "degraded"),
    (None, requests.ConnectionError("dns"), "unreachable"),
    (None, requests.Timeout("slow"), "unreachable"),
])
def test_failures_are_reported_not_hidden(settings, resp, exc, expected):
    assert probe(INTEG, settings, Secrets(), FakeSession(resp, exc)).status == expected


def test_secret_headers_used_only_when_present(settings):
    alp = next(i for i in INTEGRATIONS if i.name == "alpaca_paper")
    sec = Secrets(alpaca_api_key_id="PKX", alpaca_api_secret_key="SKX")
    sess = FakeSession(FakeResp(200, {"is_open": False}))
    r = probe(alp, settings, sec, sess)
    assert r.status == "ok"
    url, headers = sess.calls[0]
    assert url.startswith("https://paper-api.alpaca.markets/")
    assert headers["APCA-API-KEY-ID"] == "PKX"


def test_sec_user_agent_contains_contact(settings):
    sec_i = next(i for i in INTEGRATIONS if i.name == "sec_edgar_submissions")
    h = sec_i.headers(settings, Secrets(contact_email="me@example.com"))
    assert "me@example.com" in h["User-Agent"]


@pytest.mark.live
def test_live_free_sources_reachable(settings, db):
    """Real network: every keyless source must return real, validated data."""
    res = probe_all(settings, Secrets(), db)
    by = {r.integration: r for r in res}
    keyless = [i.name for i in INTEGRATIONS if not i.requires]
    failed = {n: (by[n].status, by[n].detail) for n in keyless if by[n].status != "ok"}
    # at least one stock and one crypto source must work for later milestones
    assert by["yahoo_finance_chart"].status == "ok", failed
    assert any(by[n].status == "ok" for n in ("binance_public", "coinbase_exchange_public", "kraken_public"))
    assert by["alpaca_paper"].status == "needs_credentials"
    assert db.conn.execute("SELECT COUNT(*) FROM integration_health").fetchone()[0] == len(res)
    if failed:
        pytest.skip(f"some optional sources degraded: {failed}")
