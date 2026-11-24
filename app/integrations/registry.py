"""Integration registry + connectivity/health probes.

Milestone 1 only verifies *reachability* of each source and records the
result in integration_health. Actual data collection starts in milestone 2.
Each probe makes one small, rate-limit-friendly public request and checks
that the response really contains the expected data (no fake "ok").
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import requests

from app.config import Secrets, Settings
from app.core.logging_setup import get_logger
from app.core.safety import assert_url_allowed
from app.database.db import Database, utcnow

log = get_logger("integrations")


@dataclass
class ProbeResult:
    integration: str
    category: str
    status: str               # ok | degraded | unreachable | needs_credentials | disabled
    latency_ms: float | None = None
    http_status: int | None = None
    detail: str = ""
    sample: dict = field(default_factory=dict)


@dataclass
class Integration:
    name: str
    category: str             # market_data_stock | market_data_crypto | news | filings | macro | social | execution
    url: str
    validate: Callable[[requests.Response], tuple[bool, str, dict]]
    requires: tuple[str, ...] = ()     # Secrets field names
    headers: Callable[[Settings, Secrets], dict] | None = None
    free: bool = True
    notes: str = ""


# ---------- validators: each returns (ok, detail, sample) ----------
def _v_yahoo(r):
    j = r.json()["chart"]["result"][0]
    m = j["meta"]
    return (m.get("regularMarketPrice") is not None,
            f"{m.get('symbol')} price={m.get('regularMarketPrice')} exch={m.get('exchangeName')}",
            {"symbol": m.get("symbol"), "price": m.get("regularMarketPrice"),
             "market_time": m.get("regularMarketTime"), "candles": len(j.get("timestamp") or [])})


def _v_binance(r):
    j = r.json()
    p = float(j["price"])
    return p > 0, f"BTCUSDT={p}", {"symbol": j["symbol"], "price": p}


def _v_coinbase(r):
    j = r.json()
    p = float(j["price"])
    return p > 0, f"BTC-USD={p} bid={j.get('bid')} ask={j.get('ask')}", {
        "price": p, "bid": j.get("bid"), "ask": j.get("ask"), "time": j.get("time")}


def _v_kraken(r):
    j = r.json()
    if j.get("error"):
        return False, str(j["error"]), {}
    k = next(iter(j["result"].values()))
    return float(k["c"][0]) > 0, f"XBTUSD last={k['c'][0]}", {"last": k["c"][0]}


def _v_coingecko(r):
    j = r.json()
    p = j.get("bitcoin", {}).get("usd")
    return bool(p), f"bitcoin usd={p}", {"usd": p, "last_updated_at": j.get("bitcoin", {}).get("last_updated_at")}


def _v_sec_submissions(r):
    j = r.json()
    n = len(j.get("filings", {}).get("recent", {}).get("form", []))
    return n > 0, f"{j.get('name')} recent filings={n}", {"name": j.get("name"), "recent_count": n}


def _v_rss(r):
    t = r.text
    n = t.count("<item") + t.count("<entry")
    return n > 0, f"items={n}", {"items": n}


def _v_alpaca(r):
    j = r.json()
    return "is_open" in j, f"market is_open={j.get('is_open')}", {"is_open": j.get("is_open")}


def _v_x(r):
    j = r.json()
    return "data" in j or "meta" in j, "search ok", {}


def _sec_headers(settings: Settings, secrets: Secrets) -> dict:
    contact = secrets.contact_email or "atlas-paper-research@localhost"
    return {"User-Agent": f"ATLAS-Trading-AI research {contact}", "Accept-Encoding": "gzip, deflate"}


def _alpaca_headers(settings, secrets) -> dict:
    return {"APCA-API-KEY-ID": secrets.alpaca_api_key_id.get_secret_value(),
            "APCA-API-SECRET-KEY": secrets.alpaca_api_secret_key.get_secret_value()}


def _x_headers(settings, secrets) -> dict:
    return {"Authorization": f"Bearer {secrets.x_bearer_token.get_secret_value()}"}


INTEGRATIONS: list[Integration] = [
    Integration("yahoo_finance_chart", "market_data_stock",
                "https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=1d&interval=5m",
                _v_yahoo, notes="Unofficial public endpoint; intraday usually ~real-time for US equities but not guaranteed; treat as 'delayed' unless timestamp proves otherwise."),
    Integration("binance_public", "market_data_crypto",
                "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT", _v_binance,
                notes="Public market-data mirror; no key. Order book via /api/v3/depth."),
    Integration("coinbase_exchange_public", "market_data_crypto",
                "https://api.exchange.coinbase.com/products/BTC-USD/ticker", _v_coinbase,
                notes="Public ticker incl. bid/ask; no key."),
    Integration("kraken_public", "market_data_crypto",
                "https://api.kraken.com/0/public/Ticker?pair=XBTUSD", _v_kraken,
                notes="Public ticker; no key. Private endpoints blocked in code."),
    Integration("coingecko_free", "market_data_crypto",
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_last_updated_at=true",
                _v_coingecko, notes="Keyless public tier, tight rate limits (~5-30 req/min). Fundamentals: market cap, supply."),
    Integration("sec_edgar_submissions", "filings",
                "https://data.sec.gov/submissions/CIK0000320193.json", _v_sec_submissions,
                headers=_sec_headers, notes="Free; SEC requires a descriptive User-Agent with contact e-mail (ATLAS_CONTACT_EMAIL). Max 10 req/s."),
    Integration("sec_edgar_8k_feed", "filings",
                "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=10&output=atom",
                _v_rss, headers=_sec_headers, notes="Latest 8-K filings Atom feed (company announcements)."),
    Integration("sec_edgar_form4_feed", "filings",
                "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&count=10&output=atom",
                _v_rss, headers=_sec_headers, notes="Insider transaction disclosures (Form 4) - public filings only."),
    Integration("federal_reserve_rss", "macro",
                "https://www.federalreserve.gov/feeds/press_all.xml", _v_rss,
                notes="Fed press releases (FOMC decisions)."),
    Integration("marketwatch_rss", "news",
                "https://feeds.content.dowjones.io/public/rss/mw_topstories", _v_rss,
                notes="Headlines only; respect publisher terms."),
    Integration("coindesk_rss", "news",
                "https://www.coindesk.com/arc/outboundfeeds/rss/", _v_rss,
                notes="Crypto news headlines."),
    Integration("alpaca_paper", "execution",
                "https://paper-api.alpaca.markets/v2/clock", _v_alpaca,
                requires=("alpaca_api_key_id", "alpaca_api_secret_key"), headers=_alpaca_headers,
                notes="Free paper account; needs sign-up + API keys. PAPER endpoint only."),
    Integration("x_api_v2", "social",
                "https://api.x.com/2/tweets/search/recent?query=%24SPY&max_results=10", _v_x,
                requires=("x_bearer_token",), headers=_x_headers, free=False,
                notes="Search requires a paid/credited X API tier. Social engine disabled until configured."),
]


def probe(integ: Integration, settings: Settings, secrets: Secrets,
          session: requests.Session | None = None) -> ProbeResult:
    missing = [r for r in integ.requires if not secrets.has(r)]
    if missing:
        return ProbeResult(integ.name, integ.category, "needs_credentials",
                           detail=f"missing: {', '.join(missing)}")
    assert_url_allowed(integ.url)
    s = session or requests.Session()
    headers = {"User-Agent": settings.http.user_agent}
    if integ.headers:
        headers.update(integ.headers(settings, secrets))
    t0 = time.perf_counter()
    try:
        r = s.get(integ.url, headers=headers, timeout=settings.http.timeout_seconds)
    except requests.RequestException as e:
        return ProbeResult(integ.name, integ.category, "unreachable",
                           latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                           detail=f"{type(e).__name__}: {str(e)[:200]}")
    latency = round((time.perf_counter() - t0) * 1000, 1)
    if r.status_code in (401, 403):
        st = "needs_credentials" if integ.requires else "degraded"
        return ProbeResult(integ.name, integ.category, st, latency, r.status_code,
                           f"HTTP {r.status_code}")
    if r.status_code == 429:
        return ProbeResult(integ.name, integ.category, "degraded", latency, 429, "rate limited")
    if r.status_code != 200:
        return ProbeResult(integ.name, integ.category, "unreachable", latency, r.status_code,
                           f"HTTP {r.status_code}")
    try:
        ok, detail, sample = integ.validate(r)
    except Exception as e:  # malformed payload = data integrity problem
        return ProbeResult(integ.name, integ.category, "degraded", latency, r.status_code,
                           f"invalid payload: {type(e).__name__}: {e}")
    return ProbeResult(integ.name, integ.category, "ok" if ok else "degraded", latency,
                       r.status_code, detail, sample)


def probe_all(settings: Settings, secrets: Secrets, db: Database | None = None,
              only: list[str] | None = None) -> list[ProbeResult]:
    out = []
    with requests.Session() as s:
        for integ in INTEGRATIONS:
            if only and integ.name not in only:
                continue
            res = probe(integ, settings, secrets, s)
            out.append(res)
            log.info("probe %s -> %s (%s)", res.integration, res.status, res.detail,
                     extra={"ctx": {"latency_ms": res.latency_ms, "http": res.http_status}})
            if db is not None:
                db.insert("integration_health", {
                    "ts": utcnow(), "integration": res.integration, "category": res.category,
                    "status": res.status, "latency_ms": res.latency_ms,
                    "http_status": res.http_status, "detail": res.detail[:500]})
    return out
