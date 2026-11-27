"""Keep the state DB under GitHub's 100 MB file limit: drop the raw intraday candle cache older than N days
(only used as a stale fallback; backtests use var/history) and old per-symbol scan summaries (the dashboard
shows 24 h), then VACUUM. Trades, signals, reviews, risk journal and news are never pruned."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def prune(db, days: float = 3.0, now: datetime | None = None) -> dict:
    cut = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
    c1 = db.conn.execute("DELETE FROM market_observations WHERE kind='candle' AND timeframe IN ('1m','5m','15m','1h') "
                         "AND source_ts < ?", (cut,)).rowcount
    c2 = db.conn.execute("UPDATE scan_runs SET summary_json=NULL WHERE started_ts < ? AND summary_json IS NOT NULL",
                         (cut,)).rowcount
    db.conn.commit()
    db.conn.execute("VACUUM")
    return {"candles_deleted": c1, "scan_summaries_cleared": c2}
