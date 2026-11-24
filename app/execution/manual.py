"""Owner-requested MANUAL paper trade (DECISIONS D87).

Only for an explicit request by the owner. Strategy label `manual_news`, validation_status `manual`
(so it is NOT counted in forward-test stats and never mistaken for a validated strategy).
The normal risk engine runs with ONLY `MANUAL_WAIVABLE` soft filters waived (validation status, score);
every hard limit still applies: $2.50 risk / trade (0.25 %), $10 daily stop, max 2 positions, no leverage,
valid stop + target, net R:R >= 2 after costs, reliable fresh quote, sessions, SAFE STATE.
The open position is then managed by the regular loop (stop / target / time exit) like any other.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.data.models import AssetClass
from app.risk.engine import MANUAL_WAIVABLE, Proposal

STRATEGY = "manual_news"


def manual_trade(trader, symbol: str, stop: float, target: float, news_id: int, note: str = "",
                 now: datetime | None = None, dry_run: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    db = trader.db
    news = db.conn.execute("SELECT id, published_ts, source, title, verified FROM news_events WHERE id=?",
                           (news_id,)).fetchone()
    if news is None:
        raise ValueError(f"news_events.id {news_id} not found - a manual trade must cite real ingested news")
    ac = AssetClass.CRYPTO if symbol.endswith("-USD") else AssetClass.STOCK
    q = trader.quote(symbol, now)
    if q is None:
        raise RuntimeError(f"no reliable quote for {symbol}")
    p = Proposal(None, symbol, ac, STRATEGY, "long", q.price, stop, target, None, "new", None, "live", "manual")
    d = trader.risk.evaluate(p, q, now, waive=MANUAL_WAIVABLE)
    out = {"symbol": symbol, "approved": d.approved, "reason": d.reason, "evaluation_id": d.evaluation_id,
           "quote": q.price, "expected_fill": d.expected_fill, "qty": d.quantity, "notional": d.notional,
           "planned_risk_usd": d.planned_risk_usd, "rr_net": d.rr_net, "stop": stop, "target": target,
           "waived": [c.name + ": " + c.detail for c in d.checks if c.detail.startswith("WAIVED")],
           "news": {"id": news["id"], "published_ts": news["published_ts"], "source": news["source"],
                    "title": news["title"], "verified": bool(news["verified"])}}
    if not d.approved or dry_run:
        return out
    out["fill"] = trader.sim.open_position(p, d, q, now)
    db.log_event("manual_trade", "execution",
                 f"{symbol} manual_news (owner request) news #{news_id}: {news['title'][:120]}", "warning",
                 {"evaluation_id": d.evaluation_id, "news_id": news_id, "note": note, "waived": out["waived"]})
    if trader.mirror is not None and trader.mirror.enabled:
        f = out["fill"]
        out["alpaca"] = trader.mirror.open(symbol, ac.value, f["quantity"], f["fill_price"], stop, target,
                                           f["client_order_id"], f["order_id"], trader.ledger.state(now).equity)
    return out
