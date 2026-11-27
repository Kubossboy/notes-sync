"""D104 post-trade review: features at entry + rule-tagged loss causes (+ optional one-sentence AI lesson)."""
from __future__ import annotations

import json
from datetime import datetime

from app.data.calendar import ET

CAUSE_TEXT = {"gap": "price gapped through the stop", "noise": "stopped by normal noise (stop too tight)",
              "against_trend": "traded against the bigger trend", "news": "negative news",
              "late_entry": "late entry (chased an extended move)", "unclear": "no clear mistake (normal loss)"}


def features(pos: dict, ev: dict, sig_entry: float | None, verdict: dict | None) -> dict:
    f = dict(ev.get("features") or {})
    news = ev.get("news") or {}
    reg = ev.get("regime") or f.get("regime")
    opened = datetime.fromisoformat(pos["opened_ts"])
    entry, stop = pos["entry_price"], pos.get("initial_stop") or pos["stop_loss"]
    atr = f.get("atr14") or ev.get("atr10")
    return {
        "strategy": pos["strategy"], "regime": f"{reg['trend']}/{reg['vol']}" if isinstance(reg, dict) and reg.get("trend") else reg,
        "rsi": f.get("rsi14", ev.get("rsi", ev.get("rsi2"))), "atr": atr, "rvol": f.get("rvol20", ev.get("rvol")),
        "stop_atr": round((entry - stop) / atr, 2) if atr else None,
        "res_dist_atr": f.get("res_dist_atr"), "trend": ev.get("trend"), "market_trend": ev.get("market_trend"),
        "hour_ny": opened.astimezone(ET).hour, "finbert": news.get("aggregate") if isinstance(news, dict) else None,
        "qwen": verdict.get("decision") if verdict else None,
        "entry_drift_r": round((entry - sig_entry) / (entry - stop), 2) if sig_entry and entry > stop else None,
        "hold_min": round((datetime.fromisoformat(pos["closed_ts"]) - opened).total_seconds() / 60, 1)
        if pos.get("closed_ts") else None}


def causes(pos: dict, f: dict, neg_news: bool) -> list[str]:
    if (pos.get("r_multiple") or 0) > 0:
        return []
    out = []
    stop = pos.get("initial_stop") or pos["stop_loss"]
    if pos.get("exit_reason") in ("stop_loss", "liquidation") and pos.get("exit_price") and \
            pos["exit_price"] < min(stop, pos["stop_loss"]) * 0.997:
        out.append("gap")
    if pos.get("exit_reason") == "stop_loss" and (
            (f.get("stop_atr") is not None and f["stop_atr"] < 1.0) or (f.get("hold_min") or 1e9) < 20):
        out.append("noise")
    if "bear" in str(f.get("regime") or "") or f.get("trend") == "down" or f.get("market_trend") == "down":
        out.append("against_trend")
    if neg_news or (isinstance(f.get("finbert"), (int, float)) and f["finbert"] <= -0.15):
        out.append("news")
    if (f.get("rsi") or 0) >= 70 or (f.get("entry_drift_r") or 0) > 0.15 or \
            (f.get("res_dist_atr") is not None and f["res_dist_atr"] < 0.5):
        out.append("late_entry")
    return out or ["unclear"]


def _neg_news(db, sym: str, ac: str, t0: str, t1: str) -> bool:
    tick = sym.split("-")[0] if ac == "crypto" else sym
    r = db.conn.execute("SELECT 1 FROM news_events WHERE (tickers LIKE ? OR tickers LIKE ?) AND sentiment <= -0.3 "
                        "AND COALESCE(published_ts, fetched_ts) BETWEEN ? AND ? LIMIT 1",
                        (f"%,{tick},%", f"%,{sym},%", t0, t1)).fetchone()
    return r is not None


def add_lessons(db, llm, max_lessons: int = 1) -> int:
    """Ask the local model for a one-sentence lesson on the newest un-annotated main-ledger trades (budgeted)."""
    from app.llm import lesson
    done = 0
    for r in db.conn.execute("SELECT * FROM trade_reviews WHERE ledger='main' AND lesson IS NULL "
                             "ORDER BY id DESC LIMIT ?", (max_lessons,)).fetchall():
        tags = ", ".join(CAUSE_TEXT.get(c, c) for c in (r["causes"] or "").split(",") if c) or "win"
        txt = lesson(llm, f"{r['symbol']} {r['strategy']} long, result {r['r_multiple'] or 0:+.2f} R, exit "
                          f"{r['exit_reason']}. Features: {r['features_json']}. Rule tags: {tags}.")
        if not txt:
            break
        db.conn.execute("UPDATE trade_reviews SET lesson=? WHERE id=?", (txt, r["id"]))
        done += 1
    return done


def review_closed(db, settings, now: datetime, limit: int = 200) -> list[dict]:
    """Review every closed main/shadow trade not reviewed yet. Returns the new reviews."""
    verdicts = {v.get("signal_id"): v for v in (db.get_state("llm_verdicts") or [])}
    out = []
    q = {"main": "SELECT p.*, s.evidence_json, s.entry_price AS sig_entry FROM positions p LEFT JOIN signals s "
                 "ON s.id = p.signal_id WHERE p.status='closed' AND NOT EXISTS (SELECT 1 FROM trade_reviews r WHERE "
                 "r.ledger='main' AND r.position_id=p.id) ORDER BY p.id LIMIT ?",
         "shadow": "SELECT p.*, p.stop_loss AS stop_loss, s.evidence_json, s.entry_price AS sig_entry FROM "
                   "shadow_positions p LEFT JOIN signals s ON s.id = p.signal_id WHERE p.status='closed' AND NOT EXISTS "
                   "(SELECT 1 FROM trade_reviews r WHERE r.ledger='shadow' AND r.position_id=p.id) ORDER BY p.id LIMIT ?"}
    for ledger, sql in q.items():
        for row in db.conn.execute(sql, (limit,)).fetchall():
            pos = dict(row)
            try:
                ev = json.loads(pos.get("evidence_json") or "{}")
            except ValueError:
                ev = {}
            f = features(pos, ev, pos.get("sig_entry"), verdicts.get(pos.get("signal_id")))
            neg = _neg_news(db, pos["symbol"], pos["asset_class"], pos["opened_ts"], pos["closed_ts"] or now.isoformat())
            cs = causes(pos, f, neg)
            r = pos.get("r_multiple")
            les = None
            rec = {"ts": now.isoformat(), "ledger": ledger, "position_id": pos["id"], "symbol": pos["symbol"],
                   "asset_class": pos["asset_class"], "strategy": pos["strategy"], "regime": f.get("regime"),
                   "outcome": "win" if (r or 0) > 0 else "loss", "r_multiple": r, "exit_reason": pos.get("exit_reason"),
                   "features_json": json.dumps(f, default=str), "causes": ",".join(cs), "lesson": les}
            db.insert("trade_reviews", rec)
            out.append(rec)
    return out
