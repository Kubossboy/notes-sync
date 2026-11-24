"""NEWS INTELLIGENCE ENGINE: ingest -> dedupe -> tag -> catalysts -> sentiment
-> verify (story clustering) -> journal. Every event keeps source, publication
time and a confidence value."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from app.config import PROJECT_ROOT, Secrets, Settings
from app.core.logging_setup import get_logger
from app.core.safety import SafeState
from app.database.db import Database, utcnow
from app.intelligence import catalysts as CAT
from app.intelligence.feeds import RawItem, fetch_feed
from app.intelligence.sec import FORM_MAP, ITEM_MAP, UNEXPECTED_ITEMS, Filing, SecClient
from app.intelligence.sentiment_lexicon import lexicon_sentiment
from app.intelligence.sources import FEEDS, TIER_CONFIDENCE, FeedSource
from app.intelligence.tagging import TickerTagger
from app.intelligence.verify import content_hash, same_story, time_quality, verification_status
from app.intelligence.x_source import XSource

log = get_logger("news")

# Bump when tagging / catalyst / sentiment rules change: items are then re-analysed
# (new rows; the content hash includes the version) and context reads only this version.
ANALYZER_VERSION = 3


def event_hash(source: str, title: str, url: str | None) -> str:
    return content_hash(f"{source}#a{ANALYZER_VERSION}", title, url)


@dataclass
class Event:
    source: str
    tier: int
    scope: str
    title: str
    url: str | None
    published: datetime | None
    summary: str
    tags: dict[str, float]
    catalysts: list[CAT.Catalyst]
    unexpected: bool
    lexicon: float
    sentiment: float
    direction: float
    impact: str | None
    confidence: float
    time_flags: list[str] = field(default_factory=list)
    category: str | None = None
    external_id: str | None = None
    hash: str = ""


class NewsEngine:
    def __init__(self, settings: Settings, db: Database, secrets: Secrets | None = None,
                 safe_state: SafeState | None = None, session: requests.Session | None = None,
                 feeds: list[FeedSource] | None = None, sec: SecClient | None = None):
        self.s, self.cfg, self.db = settings, settings.news, db
        self.secrets = secrets or Secrets()
        self.safe = safe_state
        self.session = session or requests.Session()
        self.feeds = FEEDS if feeds is None else feeds
        self.sec = sec or SecClient(self.session, self.secrets.contact_email, PROJECT_ROOT / "var",
                                    settings.http.timeout_seconds)
        self.x = XSource(self.secrets, self.session, settings.http.timeout_seconds)
        self.stocks = settings.markets.stocks.watchlist
        self.cryptos = settings.markets.crypto.watchlist
        self._tagger: TickerTagger | None = None
        self.ua = f"ATLAS-Trading-AI/0.4 (research; {self.secrets.contact_email or 'no-contact'})"

    # ------------------------------------------------------------ tagging
    @property
    def tagger(self) -> TickerTagger:
        if self._tagger is None:
            names = {}
            try:
                names = {t: self.sec.company_name(t) for t in self.stocks}
            except Exception as e:     # SEC map unavailable -> curated aliases only
                log.warning("SEC ticker map unavailable: %s", e)
            self._tagger = TickerTagger(self.stocks, self.cryptos, names)
        return self._tagger

    # ------------------------------------------------------------ analysis
    def analyze(self, raw: RawItem, src: FeedSource, now: datetime) -> Event:
        text = f"{raw.title}. {raw.summary}"
        tags = self.tagger.tag_doc(raw.title, raw.summary)
        cats, unexpected = CAT.detect(text)
        if src.default_category == "exchange" and not cats:
            cats = [CAT.Catalyst("exchange_status", 0, "low", "status page entry")]
        lex, _ = lexicon_sentiment(text)
        direction = CAT.net_direction(cats)
        if src.default_category == "exchange":     # status-page prose: lexicon is noise, rules only
            sentiment = direction
        else:
            sentiment = 0.6 * direction + 0.4 * lex if any(c.direction for c in cats) else lex
        tq, flags = time_quality(raw.published, now)
        own = [v for k, v in tags.items() if k not in ("MACRO", "CRYPTO_MARKET")]
        tag_conf = max(own) if own else (max(tags.values()) if tags else 1.0)
        conf = round(TIER_CONFIDENCE[src.tier] * tq * tag_conf, 3)
        category = cats[0].category if cats else src.default_category
        return Event(src.name, src.tier, src.scope, raw.title, raw.url, raw.published, raw.summary, tags, cats,
                     unexpected, lex, round(max(-1, min(1, sentiment)), 4), round(direction, 3),
                     CAT.max_impact(cats), conf, flags, category, raw.guid,
                     event_hash(src.name, raw.title, raw.url))

    def filing_event(self, f: Filing, now: datetime, insider: list[dict] | None = None) -> Event:
        cats, unexpected = [], False
        labels = []
        for it in f.items:
            if it in ITEM_MAP:
                c, d, imp = ITEM_MAP[it]
                cats.append(CAT.Catalyst(c, d, imp, f"8-K item {it}"))
                labels.append(f"{it} {c}")
                unexpected |= it in UNEXPECTED_ITEMS
        if not f.items and f.form in FORM_MAP:
            c, d, imp = FORM_MAP[f.form]
            cats.append(CAT.Catalyst(c, d, imp, f"form {f.form}"))
        if insider:
            buys = sum(t["shares"] * t["price"] for t in insider if t["code"] == "P")
            sells = sum(t["shares"] * t["price"] for t in insider if t["code"] == "S")
            cats = []
            if buys > 0:
                cats.append(CAT.Catalyst("insider_buy", 1, "medium" if buys >= 50_000 else "low",
                                         f"open-market purchases ${buys:,.0f}"))
            if sells > 0:
                planned = any(t["plan_10b5_1"] for t in insider)
                cats.append(CAT.Catalyst("insider_sell", -1, "low", f"open-market sales ${sells:,.0f}"
                                         + (" (10b5-1 plan)" if planned else "")))
            labels.append(f"buys ${buys:,.0f} sells ${sells:,.0f}")
        title = f"{f.ticker} {f.form}: {f.description}" + (f" [{'; '.join(labels)}]" if labels else "")
        direction = CAT.net_direction(cats)
        tq, flags = time_quality(f.accepted, now, max_age_h=24 * self.cfg.filings_lookback_days)
        return Event("sec_edgar", 1, "stock", title, f.url, f.accepted, "", {f.ticker: 1.0}, cats, unexpected,
                     0.0, direction, round(direction, 3), CAT.max_impact(cats),
                     round(TIER_CONFIDENCE[1] * tq, 3), flags, cats[0].category if cats else f.form,
                     f.accession, event_hash("sec_edgar", f.accession, f.url))

    # ------------------------------------------------------------ persistence
    def _recent_for_clustering(self, now: datetime) -> list[dict]:
        since = (now - timedelta(hours=36)).isoformat()
        return [dict(r) for r in self.db.conn.execute(
            "SELECT id, title, source, published_ts, story_id FROM news_events "
            "WHERE COALESCE(published_ts, fetched_ts) >= ? AND analyzer_version=?", (since, ANALYZER_VERSION))]

    def store(self, ev: Event, recent: list[dict], now: datetime) -> int | None:
        if self.db.conn.execute("SELECT 1 FROM news_events WHERE content_hash=?", (ev.hash,)).fetchone():
            return None
        story = None
        for r in recent:
            ts = datetime.fromisoformat(r["published_ts"]) if r["published_ts"] else None
            if r["source"] != ev.source and same_story(ev.title, ev.published, r["title"], ts,
                                                       self.cfg.story_similarity):
                story = r["story_id"] or r["id"]
                break
        tickers = "," + ",".join(sorted(ev.tags)) + "," if ev.tags else ""
        with self.db.transaction():
            nid = self.db.insert("news_events", {
                "fetched_ts": now.isoformat(), "published_ts": ev.published.isoformat() if ev.published else None,
                "source": ev.source, "url": ev.url, "title": ev.title[:500], "summary": ev.summary[:1000],
                "tickers": tickers, "category": ev.category, "confidence": ev.confidence,
                "content_hash": ev.hash, "source_tier": ev.tier, "scope": ev.scope, "story_id": story,
                "sentiment": ev.sentiment, "direction": ev.direction, "impact": ev.impact,
                "catalysts_json": json.dumps([c.__dict__ for c in ev.catalysts]), "unexpected": int(ev.unexpected),
                "time_flags": ",".join(ev.time_flags) or None, "tag_conf_json": json.dumps(ev.tags),
                "external_id": ev.external_id, "analyzer_version": ANALYZER_VERSION})
            if story is None:
                self.db.conn.execute("UPDATE news_events SET story_id=? WHERE id=?", (nid, nid))
                story = nid
            # re-verify the whole story: primary source or >= 2 independent sources
            rows = self.db.conn.execute("SELECT id, source, source_tier FROM news_events WHERE story_id=?",
                                        (story,)).fetchall()
            n_src = len({r["source"] for r in rows if r["source_tier"] <= 3})
            best_tier = min(r["source_tier"] for r in rows)
            ok, why = verification_status(best_tier, n_src)
            self.db.conn.execute("UPDATE news_events SET verified=?, verification=? WHERE story_id=?",
                                 (int(ok), why, story))
            for t in ev.tags:
                if t in ("MACRO", "CRYPTO_MARKET"):
                    continue
                self.db.insert("sentiment_results", {
                    "ts": now.isoformat(), "symbol": t, "source": ev.source, "method": "lexicon+catalyst_rules",
                    "score": ev.sentiment, "confidence": ev.confidence, "news_event_id": nid,
                    "details_json": json.dumps({"lexicon": ev.lexicon, "direction": ev.direction,
                                                "catalysts": [c.category for c in ev.catalysts]})})
        recent.append({"id": nid, "title": ev.title, "source": ev.source,
                       "published_ts": ev.published.isoformat() if ev.published else None, "story_id": story})
        return nid

    # ------------------------------------------------------------ ingest
    def ingest(self, now: datetime | None = None) -> dict:
        now_is_wall = now is None
        now = now or datetime.now(timezone.utc)
        run = self.db.insert("news_ingest_runs", {"started_ts": now.isoformat()})
        recent = self._recent_for_clustering(now)
        stats = {"sources": {}, "seen": 0, "new": 0, "analyzer_version": ANALYZER_VERSION}
        ok = failed = 0

        def record(name, status, seen=0, new=0, err=None):
            stats["sources"][name] = {"status": status, "seen": seen, "new": new, **({"error": err} if err else {})}

        for src in self.feeds:
            urls = [src.url.format(tickers=t) for t in self.stocks] if src.per_ticker else [src.url]
            try:
                items, errs = [], []
                for url in urls:
                    try:
                        items += fetch_feed(url, src.name, self.session, self.ua, self.s.http.timeout_seconds)
                    except Exception as e:      # one ticker failing is not a source failure
                        errs.append(e)
                if errs and len(errs) == len(urls):
                    raise errs[0]
            except Exception as e:
                failed += 1
                record(src.name, "failed", err=f"{type(e).__name__}: {str(e)[:150]}")
                self.db.log_event("news_source_failed", "news", f"{src.name}: {e}", "warning")
                continue
            ok += 1
            new = 0
            for raw in items:
                if self.store(self.analyze(raw, src, now), recent, now):
                    new += 1
            stats["seen"] += len(items); stats["new"] += new
            record(src.name, "ok", len(items), new)

        # SEC filings + Form 4 for watchlist stocks (primary source)
        since = now - timedelta(days=self.cfg.filings_lookback_days)
        sec_seen = sec_new = 0
        try:
            for t in self.stocks:
                filings = self.sec.recent_filings(t, since)
                f4 = 0
                for f in filings:
                    insider = None
                    if f.form == "4":
                        if f4 >= self.cfg.form4_per_ticker:
                            continue
                        f4 += 1
                        insider = self.sec.form4_transactions(f)
                        self._store_insider(f, insider, now)
                    sec_seen += 1
                    if self.store(self.filing_event(f, now, insider), recent, now):
                        sec_new += 1
            ok += 1
            record("sec_edgar", "ok", sec_seen, sec_new)
        except Exception as e:
            failed += 1
            record("sec_edgar", "failed", sec_seen, sec_new, f"{type(e).__name__}: {str(e)[:150]}")
        stats["seen"] += sec_seen; stats["new"] += sec_new

        xs = self.x.status()
        if xs.enabled:
            try:
                xsrc = FeedSource("x_api_v2", "", 4, "mixed")
                q = " OR ".join([f"${t}" for t in self.stocks[:8]])
                items = self.x.search(q)
                n = sum(1 for raw in items if self.store(self.analyze(raw, xsrc, now), recent, now))
                ok += 1; record("x_api_v2", "ok", len(items), n)
            except Exception as e:
                failed += 1; record("x_api_v2", "failed", err=str(e)[:150])
        else:
            record("x_api_v2", "needs_credentials", err=xs.detail)

        stats["ok"], stats["failed"] = ok, failed
        self.db.conn.execute(
            "UPDATE news_ingest_runs SET finished_ts=?, sources_ok=?, sources_failed=?, items_seen=?, items_new=?, "
            "details_json=? WHERE id=?", (datetime.now(timezone.utc).isoformat() if now_is_wall else now.isoformat(), ok, failed, stats["seen"], stats["new"], json.dumps(stats), run))
        if ok == 0 and self.safe is not None:
            self.safe.halt("news intelligence: every news source failed", module="news")
        log.info("news ingest: %d ok, %d failed, %d seen, %d new", ok, failed, stats["seen"], stats["new"])
        return stats

    def _store_insider(self, f: Filing, txs: list[dict], now: datetime) -> None:
        for i, t in enumerate(txs):
            self.db.conn.execute(
                "INSERT OR IGNORE INTO insider_transactions (ts, ticker, accession, line, owner, title, is_director, "
                "is_officer, code, shares, price, acq_disp, tx_date, plan_10b5_1) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now.isoformat(), f.ticker, f.accession, i, t["owner"], t["title"], int(t["is_director"]),
                 int(t["is_officer"]), t["code"], t["shares"], t["price"], t["acq_disp"], t["date"],
                 int(t["plan_10b5_1"])))

    # ------------------------------------------------------------ health & context
    def health(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        r = self.db.conn.execute("SELECT * FROM news_ingest_runs WHERE finished_ts IS NOT NULL "
                                 "ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            return {"healthy": False, "reason": "no ingest run yet", "last_run": None}
        age = (now - datetime.fromisoformat(r["finished_ts"])).total_seconds() / 60
        ver = json.loads(r["details_json"] or "{}").get("analyzer_version", 0)
        if ver != ANALYZER_VERSION:
            return {"healthy": False, "reason": f"last ingest used analyzer v{ver} (current v{ANALYZER_VERSION})",
                    "last_run": r["finished_ts"], "age_minutes": round(age, 1), "stale_version": True}
        total = (r["sources_ok"] or 0) + (r["sources_failed"] or 0)
        ratio = (r["sources_ok"] or 0) / total if total else 0
        healthy = age <= self.cfg.max_health_age_minutes and ratio >= self.cfg.min_source_success_ratio
        return {"healthy": healthy, "age_minutes": round(age, 1), "success_ratio": round(ratio, 2),
                "last_run": r["finished_ts"], "reason": None if healthy else
                ("stale ingest" if age > self.cfg.max_health_age_minutes else "too many sources failed")}

    def needs_refresh(self, now: datetime | None = None) -> bool:
        h = self.health(now)
        return (h["last_run"] is None or h.get("stale_version", False)
                or h.get("age_minutes", 1e9) >= self.cfg.refresh_minutes)

    def _events(self, tag: str, since: datetime, until: datetime) -> list[dict]:
        # upper bound: research/replay at a past `now` never sees later news (no look-ahead)
        rows = self.db.conn.execute(
            "SELECT * FROM news_events WHERE analyzer_version = ? AND tickers LIKE ? "
            "AND COALESCE(published_ts, fetched_ts) >= ? "
            "AND COALESCE(published_ts, fetched_ts) <= ? AND fetched_ts <= ? "
            "ORDER BY COALESCE(published_ts, fetched_ts) DESC",
            (ANALYZER_VERSION, f"%,{tag},%", since.isoformat(), until.isoformat(), (until + timedelta(minutes=1)).isoformat())).fetchall()
        out = []
        for r in rows:
            conf = json.loads(r["tag_conf_json"] or "{}").get(tag, 0)
            if conf >= self.cfg.min_tag_confidence:
                out.append(dict(r))
        return out

    def _aggregate(self, events: list[dict], now: datetime) -> tuple[float | None, float]:
        num = den = 0.0
        hl = self.cfg.half_life_hours
        seen_story = set()
        for e in events:
            if e["story_id"] in seen_story:   # one vote per story, not per copy
                continue
            seen_story.add(e["story_id"])
            ts = datetime.fromisoformat(e["published_ts"] or e["fetched_ts"])
            decay = 0.5 ** (max(0.0, (now - ts).total_seconds() / 3600) / hl)
            w = (e["confidence"] or 0) * decay * CAT.IMPACT_WEIGHT.get(e["impact"] or "low", 0.5) * (
                1.0 if e["verified"] else 0.8)
            num += w * (e["sentiment"] or 0)
            den += w
        return (num / den if den else None), den

    def symbol_context(self, symbol: str, asset_class: str, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        since = now - timedelta(hours=self.cfg.window_hours)
        own = self._events(symbol, since, now)
        macro = self._events("MACRO", since, now)
        market = self._events("CRYPTO_MARKET", since, now) if asset_class == "crypto" else []
        agg, weight = self._aggregate(own, now)
        magg, _ = self._aggregate(macro + market, now)
        cat_since = now - timedelta(hours=self.cfg.catalyst_hours)

        def brief(e):
            return {"id": e["id"], "title": e["title"][:160], "source": e["source"], "published": e["published_ts"],
                    "confidence": e["confidence"], "verified": bool(e["verified"]), "verification": e["verification"],
                    "sentiment": e["sentiment"], "impact": e["impact"], "unexpected": bool(e["unexpected"]),
                    "catalysts": [c["category"] for c in json.loads(e["catalysts_json"] or "[]")]}

        def ts(e):
            return datetime.fromisoformat(e["published_ts"] or e["fetched_ts"])

        neg_high = [brief(e) for e in own if e["verified"] and e["impact"] == "high" and (e["direction"] or 0) <= -0.5]
        pos = [brief(e) for e in own if ts(e) >= cat_since and (e["direction"] or 0) >= 0.5
               and e["impact"] in ("medium", "high")]
        return {"symbol": symbol, "health": self.health(now), "events": len(own), "aggregate": agg,
                "weight": round(weight, 3), "macro_aggregate": magg, "macro_events": len(macro) + len(market),
                "negative_high_verified": neg_high[:3], "positive_catalysts": pos[:5],
                "unexpected": [brief(e) for e in own if e["unexpected"]][:3],
                "top": [brief(e) for e in own[:5]],
                "x_status": self.x.status().detail}


def news_component(ctx: dict | None, cfg) -> float | None:
    """0-100 news score. None when the news engine is not healthy (-> signal incomplete)."""
    if not ctx or not ctx["health"]["healthy"]:
        return None
    if ctx["aggregate"] is None:
        return float(cfg.no_news_score)
    strength = min(1.0, ctx["weight"] / 1.5)
    return round(max(0.0, min(100.0, 50 + 50 * ctx["aggregate"] * strength)), 2)


def decay_weight(hours: float, half_life: float) -> float:
    return math.pow(0.5, hours / half_life)
