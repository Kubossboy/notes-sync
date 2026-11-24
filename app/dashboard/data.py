"""Dashboard data layer: READ-ONLY view of the ATLAS journal (spec section 9 / Milestone 7).

Opens SQLite with mode=ro, so the dashboard can never change trading state, and it has no order
or kill actions. Everything simulated is labelled SIMULATED; market data, news and integration
probes are labelled as REAL observations.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.core.safety import SafeState
from app.portfolio.ledger import Ledger
from app.quant.engine import QuantEngine, forward_test_stats


class ReadOnlyDB:
    """Database look-alike on a read-only connection. Writes go to an in-memory overlay only."""

    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._overlay: dict[str, Any] = {}

    def get_state(self, key: str, default: Any = None) -> Any:
        if key in self._overlay:
            return self._overlay[key]
        r = self.conn.execute("SELECT value_json FROM system_state WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_state(self, key: str, value: Any) -> None:      # never persisted
        self._overlay[key] = value

    def insert(self, table: str, row: dict) -> int:            # never persisted
        return 0

    def close(self) -> None:
        self.conn.close()


def _rows(db, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in db.conn.execute(sql, args).fetchall()]


def trade_stats(trades: list[dict]) -> dict:
    pnl = [t["realized_pnl_usd"] or 0.0 for t in trades]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    return {"trades": len(trades), "win_rate": round(len(wins) / len(trades), 4) if trades else None,
            "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
            "max_drawdown_usd": round(dd, 4), "avg_winner_usd": round(sum(wins) / len(wins), 4) if wins else None,
            "avg_loser_usd": round(sum(losses) / len(losses), 4) if losses else None,
            "expectancy_usd": round(sum(pnl) / len(pnl), 4) if pnl else None,
            "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None, "net_pnl_usd": round(sum(pnl), 4)}


def _group(trades, key):
    out: dict[str, list] = {}
    for t in trades:
        out.setdefault(t.get(key) or "unknown", []).append(t)
    return {k: trade_stats(v) for k, v in sorted(out.items())}


def collect(settings: Settings, db_path: Path | str | None = None, now: datetime | None = None,
            live_prices: bool = False) -> dict:
    """live_prices=True fetches recent public candles for open positions (read-only HTTP)."""
    from app.core.bootstrap import VERSION
    now = now or datetime.now(timezone.utc)
    db = ReadOnlyDB(db_path or settings.db_path)
    try:
        led = Ledger(settings, db)
        st = led.state(now)
        r = settings.risk
        limit = st.day_start_equity * r.max_daily_loss_pct / 100
        safe = SafeState(db).status()
        opens = st.open_positions
        closed = _rows(db, "SELECT * FROM positions WHERE status='closed' ORDER BY closed_ts")
        overview = {
            "label": "SIMULATED (internal paper ledger)", "equity": round(st.equity, 2), "cash": round(st.cash, 2),
            "daily_pnl": round(st.daily_pnl, 2), "total_pnl": round(st.equity - settings.portfolio.starting_equity_usd, 2),
            "open_positions": len(opens), "max_open_positions": r.max_open_positions,
            "daily_loss": round(st.daily_loss, 2), "daily_loss_limit": round(limit, 2),
            "daily_limit_reached": st.daily_loss >= limit, "consecutive_losses_today": st.consecutive_losses_today,
            "risk_per_trade": round(st.equity * r.max_risk_per_trade_pct / 100, 2), "trading_day": st.trading_day,
            "safe_state": {"halted": safe.halted, "reason": safe.reason, "since": safe.since},
            "mode": settings.execution.mode, "provider": settings.execution.provider,
            "alpaca_paper_orders_enabled": settings.execution.alpaca_paper_orders_enabled, "version": VERSION}
        portfolio = [{
            "id": p["id"], "symbol": p["symbol"], "asset_class": p["asset_class"], "strategy": p["strategy"],
            "validation_status": p.get("validation_status"), "quantity": p["quantity"],
            "entry_price": p["entry_price"], "current_price": p["mark_price"], "mark_ts": p["mark_ts"],
            "unrealized_pnl_usd": round(((p["mark_price"] or p["entry_price"]) - p["entry_price"]) * p["quantity"]
                                        - p["fees_usd"], 4),
            "stop_loss": p["stop_loss"], "take_profit": p["take_profit"], "opened_ts": p["opened_ts"],
            "planned_risk_usd": p["planned_risk_usd"]} for p in opens]
        from app.dashboard.charts import db_candles, equity_points, fetch_candles
        for p in portfolio:
            tf = "15m" if p["asset_class"] == "crypto" else "5m"
            c, tf, src = fetch_candles(p["symbol"], p["asset_class"]) if live_prices else ([], tf, "none")
            if not c:
                c, tf, src = db_candles(db, p["symbol"], tf)
            p["candles"], p["chart_tf"], p["chart_source"] = c, tf, src
            try:
                p["adjustments"] = _rows(db, "SELECT ts, kind, rule, old_value, new_value, reason FROM trade_adjustments "
                                             "WHERE ledger='main' AND position_id=? ORDER BY id DESC LIMIT 10", (p["id"],))
            except Exception:
                p["adjustments"] = []
        equity_curve = equity_points(db, st.equity, now)
        sigs = _rows(db, "SELECT s.*, sc.total_score, sc.score_on_available FROM signals s LEFT JOIN signal_scores sc "
                         "ON sc.signal_id = s.id ORDER BY s.id DESC LIMIT 40")
        scanner = []
        for s in sigs:
            ev = json.loads(s["evidence_json"] or "{}")
            scanner.append({
                "id": s["id"], "ts": s["ts"], "symbol": s["symbol"], "asset_class": s["asset_class"],
                "data_ts": s["data_ts"], "data_status": s["data_status"], "score": s["total_score"],
                "strategy": s["strategy"], "entry": s["entry_price"], "stop": s["stop_loss"],
                "target": s["take_profit"], "rr_net": s["reward_risk"], "confidence": ev.get("confidence"),
                "status": s["status"], "validation_status": s.get("validation_status"),
                "reject_reason": s["reject_reason"], "missing": ev.get("missing_components"),
                "evidence": {k: ev.get(k) for k in ("components", "catalysts", "trend", "crosscheck", "note")
                             if ev.get(k) is not None}})
        analytics = {"label": "SIMULATED trades", "all": trade_stats(closed),
                     "by_strategy": _group(closed, "strategy"), "by_asset_class": _group(closed, "asset_class"),
                     "by_validation_status": _group(closed, "validation_status")}
        fwd = {"label": "SIMULATED forward paper test (internal ledger = source of truth; Alpaca PAPER mirror only)",
               "strategies": settings.validation.forward_test_strategies,
               "target_trades": settings.validation.forward_test_target_trades, "stats": forward_test_stats(db),
               "recent": _rows(db, "SELECT closed_ts, symbol, strategy, entry_price, exit_price, pnl_usd, r_multiple, "
                                   "exit_reason FROM forward_test_trades ORDER BY id DESC LIMIT 15")}
        validation = QuantEngine(settings, db).summary()
        news = _rows(db, "SELECT published_ts, fetched_ts, source, source_tier, title, url, tickers, sentiment, "
                         "impact, verified, catalysts_json FROM news_events WHERE analyzer_version = "
                         "(SELECT MAX(analyzer_version) FROM news_events) AND (impact IN ('medium','high') "
                         "OR verified = 1) ORDER BY COALESCE(published_ts, fetched_ts) DESC LIMIT 30")
        for n in news:
            n["tickers"] = [t for t in (n["tickers"] or "").split(",") if t]
            n["catalysts"] = [c.get("category") for c in json.loads(n.pop("catalysts_json") or "[]")]
        risk = _rows(db, "SELECT ts, symbol, approved, planned_risk_usd, rr_net, reason FROM risk_evaluations "
                         "ORDER BY id DESC LIMIT 15")
        health = _rows(db, "SELECT integration, category, status, latency_ms, detail, ts FROM integration_health "
                           "WHERE id IN (SELECT MAX(id) FROM integration_health GROUP BY integration) "
                           "ORDER BY category, integration")
        last_scans = _rows(db, "SELECT scanner, mode, finished_ts, summary_json FROM scan_runs WHERE id IN "
                               "(SELECT MAX(id) FROM scan_runs GROUP BY scanner, mode)")
        missing = []
        for sc in last_scans:
            for sym in json.loads(sc.pop("summary_json") or "{}").get("symbols", []):
                if sym.get("skipped"):
                    missing.append({"scanner": sc["scanner"], "symbol": sym.get("symbol"), "reason": sym["skipped"]})
        last_news = db.conn.execute("SELECT MAX(finished_ts) FROM news_ingest_runs").fetchone()[0]
        last_obs = db.conn.execute("SELECT MAX(observed_ts) FROM market_observations").fetchone()[0]
        v = settings
        system = {
            "label": "REAL observations (probes, data fetches, news ingest)",
            "integrations": health, "failed_integrations": [h for h in health if h["status"] != "ok"],
            "last_market_data": last_obs, "last_news_ingest": last_news, "last_scans": last_scans,
            "missing_data": missing,
            "recent_errors": _rows(db, "SELECT ts, module, error_type, substr(message,1,200) AS message FROM errors "
                                       "ORDER BY id DESC LIMIT 10"),
            "active_modules": {"market_data": True, "scanner": True, "news": v.news.enabled,
                               "validation": v.validation.enabled, "risk_engine": True,
                               "paper_simulator": v.execution.provider == "internal_sim",
                               "alpaca_paper_orders": v.execution.alpaca_paper_orders_enabled,
                               "x_social": False}}
        from app.shadow.engine import ShadowEngine, strategy_names
        try:
            shadow = ShadowEngine(settings, db).summary(strategy_names())
        except Exception:                         # table missing on an old DB -> honest empty section
            shadow = []
        return {"generated_ts": now.isoformat(), "overview": overview, "portfolio": portfolio, "scanner": scanner,
                "shadow": shadow, "shadow_rules": {"min_trades": settings.validation.shadow_min_trades,
                                                   "promote_pf": settings.validation.shadow_promote_pf,
                                                   "demote_pf": settings.validation.shadow_demote_pf,
                                                   "max_dd_usd": settings.portfolio.starting_equity_usd
                                                   * settings.validation.max_drawdown_pct / 100},
                "equity_curve": equity_curve, "start_equity": settings.portfolio.starting_equity_usd,
                "analytics": analytics, "forward_test": fwd, "validation": validation, "news": news,
                "risk": risk, "system": system}
    finally:
        db.close()
