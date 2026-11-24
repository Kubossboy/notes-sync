"""NewsEngine, sentiment aggregation, scoring hookup and news momentum (offline)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.config import Secrets
from app.core.safety import SafeState
from app.data.models import AssetClass
from app.intelligence.engine import NewsEngine, news_component
from app.intelligence.sec import Filing
from app.intelligence.sources import FeedSource
from app.scanners.scoring import component_scores, news_problems
from app.sentiment.aggregate import combine, sentiment_component
from app.strategies.base import NoSetup, Setup, StrategyContext
from app.strategies.stock import news_momentum
from app.technical.indicators import compute_all
from tests.fakes import FakeResp

UTC = timezone.utc
NOW = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)


def rss(*items):
    body = "".join(f"<item><title>{t}</title><link>https://ex.test/{abs(hash(t))}</link>"
                   f"<pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate></item>" for t, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'


class RouteSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        r = self.routes.get(url)
        if r is None:
            return FakeResp(404, text="nf")
        if isinstance(r, Exception):
            raise r
        return r


class FakeSec:
    def __init__(self, filings=None, fail=False):
        self.filings, self.fail = filings or {}, fail

    def company_name(self, t):
        return {"NVDA": "NVIDIA CORP", "TSLA": "Tesla, Inc."}.get(t)

    def recent_filings(self, t, since):
        if self.fail:
            raise ConnectionError("sec down")
        return [f for f in self.filings.get(t, []) if f.accepted >= since]

    def form4_transactions(self, f):
        return [{"owner": "X", "title": "CEO", "is_director": False, "is_officer": True, "code": "P",
                 "shares": 1000, "price": 100.0, "acq_disp": "A", "date": "2026-11-23", "plan_10b5_1": False}]


FEEDS = [FeedSource("media_a", "https://a.test/rss", 3, "stock"),
         FeedSource("media_b", "https://b.test/rss", 3, "stock"),
         FeedSource("wire", "https://w.test/rss", 2, "stock")]


def engine(settings, db, routes, sec=None, feeds=FEEDS):
    return NewsEngine(settings, db, Secrets(), SafeState(db), RouteSession(routes), feeds, sec or FakeSec())


def test_ingest_dedup_tag_cluster_verify(settings, db):
    t = NOW - timedelta(hours=2)
    routes = {"https://a.test/rss": FakeResp(200, text=rss(("Nvidia beats estimates as data center revenue surges", t),
                                                         ("Tesla recalls 200,000 vehicles over steering defect", t))),
              "https://b.test/rss": FakeResp(200, text=rss(("Nvidia beats revenue estimates as data center sales surge",
                                                          t + timedelta(minutes=20)))),
              "https://w.test/rss": FakeResp(200, text=rss(("Federal Reserve holds interest rates steady", t)))}
    ne = engine(settings, db, routes)
    st = ne.ingest(NOW)
    assert st["ok"] == 4 and st["failed"] == 0 and st["new"] == 4
    assert st["sources"]["x_api_v2"]["status"] == "needs_credentials"
    rows = {r["title"][:12]: dict(r) for r in db.conn.execute("SELECT * FROM news_events")}
    a, b = rows["Nvidia beats"], [r for r in rows.values() if "sales surge" in r["title"]][0]
    assert a["story_id"] == b["story_id"]                       # same story from 2 independent sources
    assert a["verified"] == 1 and a["verification"] == "corroborated_by_2_sources"
    tsla = rows["Tesla recall"]
    assert tsla["verified"] == 0 and tsla["tickers"] == ",TSLA," and tsla["direction"] < 0
    assert ",MACRO," in rows["Federal Rese"]["tickers"]
    assert db.conn.execute("SELECT COUNT(*) FROM sentiment_results").fetchone()[0] >= 3
    # second run: nothing new (dedup), audit rows cannot be deleted
    assert ne.ingest(NOW + timedelta(minutes=1))["new"] == 0
    with pytest.raises(Exception):
        db.conn.execute("DELETE FROM news_events")


def test_symbol_context_and_components(settings, db):
    t = NOW - timedelta(hours=1)
    routes = {"https://a.test/rss": FakeResp(200, text=rss(("Nvidia beats estimates, raises guidance", t))),
              "https://b.test/rss": FakeResp(200, text=rss(("Nvidia beats estimates and raises guidance", t))),
              "https://w.test/rss": FakeResp(200, text=rss(("Tesla shares plunge after recall", t)))}
    ne = engine(settings, db, routes)
    ne.ingest(NOW)
    c = ne.symbol_context("NVDA", "stock", NOW)
    assert c["health"]["healthy"] and c["events"] == 2 and c["aggregate"] > 0.5
    assert c["positive_catalysts"] and not c["negative_high_verified"]
    assert news_component(c, settings.news) > 60
    none = ne.symbol_context("AAPL", "stock", NOW)
    assert none["aggregate"] is None and news_component(none, settings.news) == settings.news.no_news_score
    # look-ahead guard: at an earlier 'now' the news does not exist yet
    assert ne.symbol_context("NVDA", "stock", NOW - timedelta(hours=3))["events"] == 0
    # stale engine -> news component unavailable
    assert news_component(ne.symbol_context("NVDA", "stock", NOW + timedelta(hours=3)), settings.news) is None


def test_sec_filing_events_and_insider(settings, db):
    f8k = Filing("NVDA", 1045810, "8-K", "0001-26-1", NOW - timedelta(hours=3), ["1.03"], "a.htm", "8-K")
    f4 = Filing("NVDA", 1045810, "4", "0001-26-2", NOW - timedelta(hours=5), [], "x/f4.xml", "4")
    ne = engine(settings, db, {}, FakeSec({"NVDA": [f8k, f4]}), feeds=[])
    st = ne.ingest(NOW)
    assert st["sources"]["sec_edgar"] == {"status": "ok", "seen": 2, "new": 2}
    c = ne.symbol_context("NVDA", "stock", NOW)
    assert c["negative_high_verified"] and "bankruptcy" in c["negative_high_verified"][0]["catalysts"]
    assert c["unexpected"]
    assert db.conn.execute("SELECT code, shares FROM insider_transactions").fetchone()[:] == ("P", 1000.0)
    ctx = StrategyContext("NVDA", AssetClass.STOCK, "5m", pd.DataFrame(), news=c)
    assert news_problems(ctx, Setup("x", "NVDA", "long", 10, 9, 2.5, {}))   # long vetoed


def test_all_sources_failing_halts(settings, db):
    ne = engine(settings, db, {}, FakeSec(fail=True))
    st = ne.ingest(NOW)
    assert st["ok"] == 0 and st["failed"] == 4
    assert not ne.safe.trading_allowed()
    assert not ne.health(NOW)["healthy"]


def test_partial_failure_degrades_without_halt(settings, db):
    routes = {"https://a.test/rss": FakeResp(200, text=rss(("Nvidia gains", NOW))),
              "https://b.test/rss": ConnectionError("down"), "https://w.test/rss": FakeResp(500, text="x")}
    ne = engine(settings, db, routes)
    st = ne.ingest(NOW)
    assert st["ok"] == 2 and st["failed"] == 2 and ne.safe.trading_allowed()
    assert ne.health(NOW)["healthy"]                       # 50% success ratio
    assert db.conn.execute("SELECT COUNT(*) FROM system_events WHERE event_type='news_source_failed'").fetchone()[0] == 2


# ---------------------------------------------------------------- sentiment
def _ctx(agg=0.6, neg=None, healthy=True, pos=None):
    return {"health": {"healthy": healthy}, "aggregate": agg, "weight": 2.0, "macro_aggregate": 0.0,
            "negative_high_verified": neg or [], "positive_catalysts": pos or [], "x_status": "needs_credentials"}


def _df(trend="down", close=99, vwap=100):
    return pd.DataFrame([{"trend": trend, "close": close, "open": 100, "vwap": vwap, "roc10": -1.0,
                          "atr_pct": 0.5, "rvol20": 1.0}])


def test_sentiment_conflicts_and_caps():
    s = combine(_ctx(0.7), _df("down"), "down")
    assert any("positive news but bearish price action" in c for c in s["conflicts"])
    s2 = combine(_ctx(0.8, neg=[{"title": "x"}]), _df("up", 101, 100), "up", x_score=0.9)
    assert s2["composite"] == 0.0 and s2["capped"]                         # capped by negative fundamentals
    assert s2["components"]["social_x"] == 0.0                              # social clipped
    assert sentiment_component(s2) == 50.0
    off = combine(_ctx(healthy=False), _df(), "down")
    assert off["components"]["news"] is None and sentiment_component(off) is None


# ---------------------------------------------------------------- news momentum strategy
def momentum_df(last_bull=True, vol_spike=6.0):
    idx = pd.date_range("2026-11-24 14:30", periods=80, freq="5min", tz="UTC")
    r = np.random.default_rng(3)
    c = 100 + np.cumsum(r.normal(0, 0.1, 80))
    c[-1] = c[:-1].max() + 0.8 if last_bull else c[-2] - 0.5
    o = np.r_[c[0], c[:-1]]
    v = np.full(80, 1000.0); v[-1] = 1000 * vol_spike
    df = pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.05, "low": np.minimum(o, c) - 0.05,
                       "close": c, "volume": v, "session": "regular"}, index=idx)
    return compute_all(df, AssetClass.STOCK)


def test_news_momentum_fires_only_with_catalyst_and_confirmation():
    p = {"min_rvol": 2.0, "high_lookback": 6, "min_confidence": 0.6}
    cat = [{"title": "Nvidia beats", "confidence": 0.9, "catalysts": ["earnings_beat"], "verified": True}]
    df = momentum_df()
    unverified = [dict(cat[0], verified=False)]
    with pytest.raises(NoSetup, match="VERIFIED"):       # decisions need VERIFIED news (D77)
        news_momentum(StrategyContext("NVDA", AssetClass.STOCK, "5m", df, news=_ctx(0.7, pos=unverified)), p)
    s = news_momentum(StrategyContext("NVDA", AssetClass.STOCK, "5m", df, news=_ctx(0.7, pos=cat)), p)
    assert s.strategy == "news_momentum" and s.stop < s.entry and s.evidence["catalysts"]
    with pytest.raises(NoSetup, match="no fresh VERIFIED positive catalyst"):
        news_momentum(StrategyContext("NVDA", AssetClass.STOCK, "5m", df, news=_ctx(0.7)), p)
    with pytest.raises(NoSetup, match="negative"):
        news_momentum(StrategyContext("NVDA", AssetClass.STOCK, "5m", df,
                                      news=_ctx(0.7, pos=cat, neg=[{"title": "x"}])), p)
    with pytest.raises(NoSetup, match="unavailable"):
        news_momentum(StrategyContext("NVDA", AssetClass.STOCK, "5m", df, news=None), p)
    with pytest.raises(NoSetup):
        news_momentum(StrategyContext("NVDA", AssetClass.STOCK, "5m", momentum_df(last_bull=False),
                                      news=_ctx(0.7, pos=cat)), p)


def test_component_scores_include_news_and_sentiment(settings):
    df = momentum_df()
    ctx = StrategyContext("NVDA", AssetClass.STOCK, "5m", df, news=_ctx(0.5),
                          sentiment=combine(_ctx(0.5), df, "up"))
    comps = component_scores(ctx, {"score": 80}, settings.news)
    assert comps["news"] is not None and comps["sentiment"] is not None and comps["quant"] is None
    off = component_scores(StrategyContext("NVDA", AssetClass.STOCK, "5m", df), {"score": 80}, settings.news)
    assert off["news"] is None and off["sentiment"] is None


def test_analyzer_version_change_forces_reingest(settings, db, monkeypatch):
    import app.intelligence.engine as E
    routes = {"https://a.test/rss": FakeResp(200, text=rss(("Nvidia beats estimates", NOW)))}
    ne = engine(settings, db, routes, feeds=FEEDS[:1])
    assert ne.ingest(NOW)["new"] == 1 and not ne.needs_refresh(NOW)
    monkeypatch.setattr(E, "ANALYZER_VERSION", E.ANALYZER_VERSION + 1)
    assert ne.needs_refresh(NOW) and not ne.health(NOW)["healthy"]
    assert ne.symbol_context("NVDA", "stock", NOW)["events"] == 0          # old-version rows ignored
    assert ne.ingest(NOW)["new"] == 1                                       # re-analysed as a new row
    assert ne.symbol_context("NVDA", "stock", NOW)["events"] == 1
