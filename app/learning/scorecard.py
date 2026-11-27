"""D104 live strategy weighting: rolling per-strategy scorecard (last N main-ledger trades) + per-regime view.

active  -> normal size
reduced -> size x reduce_mult (avg R over the window below reduce_avg_r)
paused  -> no main-ledger entries (6 losses in a row or avg R <= pause_avg_r); the shadow ledger keeps tracking the
           strategy, and it is re-enabled once its shadow trades since the pause are profitable again.
Only ever LOWERS exposure; never raises size above normal or loosens a rule."""
from __future__ import annotations

import json
from datetime import datetime

KEY = "learning_scorecard"


def _streak(rs: list[float]) -> int:
    n = 0
    for r in rs:                       # newest first
        if r > 0:
            break
        n += 1
    return n


def update(db, cfg, now: datetime) -> dict:
    prev = (db.get_state(KEY) or {}).get("strategies", {})
    strategies = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT strategy FROM positions WHERE status='closed' AND strategy IS NOT NULL")]
    out = {}
    for st in sorted(set(strategies) | set(prev)):
        p = prev.get(st, {})
        since = p.get("since")
        rs = [r[0] or 0.0 for r in db.conn.execute(
            "SELECT r_multiple FROM positions WHERE status='closed' AND strategy=? AND closed_ts >= ? "
            "ORDER BY closed_ts DESC LIMIT ?", (st, since or "", cfg.window))]
        n, avg, streak = len(rs), (sum(rs) / len(rs) if rs else 0.0), _streak(rs)
        state, why = "active", None
        if p.get("state") == "paused":
            sh = [r[0] or 0.0 for r in db.conn.execute(
                "SELECT r_multiple FROM shadow_positions WHERE status='closed' AND strategy=? AND closed_ts >= ?",
                (st, p.get("paused_ts") or ""))]
            if len(sh) >= cfg.resume_min_shadow and sum(sh) / len(sh) > cfg.resume_avg_r:
                state, why = "active", (f"its shadow (tracking-only) trades recovered: {sum(sh) / len(sh):+.2f} R "
                                        f"per trade over {len(sh)} trades")
                since = now.isoformat()                       # judge it fresh from here
                n, avg, streak = 0, 0.0, 0
            else:
                state, why = "paused", p.get("why")
        elif streak >= cfg.pause_consec_losses:
            state, why = "paused", f"{streak} losses in a row"
        elif n >= cfg.min_trades and avg <= cfg.pause_avg_r:
            state, why = "paused", f"it lost {avg:+.2f} R per trade over its last {n} trades"
        elif n >= cfg.min_trades and avg < cfg.reduce_avg_r:
            state, why = "reduced", f"it is slightly losing ({avg:+.2f} R per trade over {n} trades)"
        rec = {"state": state, "why": why, "n": n, "avg_r": round(avg, 3), "streak": streak, "since": since,
               "mult": cfg.reduce_mult if state == "reduced" else (0.0 if state == "paused" else 1.0),
               "paused_ts": (p.get("paused_ts") if p.get("state") == "paused" and state == "paused"
                             else now.isoformat() if state == "paused" else None)}
        old = p.get("state", "active")
        if state != old:
            kind = {("active", "paused"): "pause", ("reduced", "paused"): "pause", ("paused", "active"): "resume",
                    ("active", "reduced"): "reduce", ("reduced", "active"): "restore"}.get((old, state), state)
            detail = {"pause": f"Paused {st} after {why}.", "resume": f"Re-enabled {st} because {why}.",
                      "reduce": f"Halved the size of {st} because {why}.",
                      "restore": f"Back to normal size for {st} (results recovered)."}.get(kind, f"{st}: {state}")
            db.insert("learning_events", {"ts": now.isoformat(), "kind": kind, "strategy": st, "detail": detail,
                                          "data_json": json.dumps({"n": n, "avg_r": round(avg, 3), "streak": streak})})
            db.log_event("learning_" + kind, "learning", detail[:300], "warning" if kind == "pause" else "info")
        out[st] = rec
    regimes = {}
    for r in db.conn.execute("SELECT strategy, regime, r_multiple FROM trade_reviews WHERE ledger='main' AND regime "
                             "IS NOT NULL ORDER BY id DESC LIMIT 400"):
        c = regimes.setdefault(f"{r[0]}|{r[1]}", [])
        if len(c) < cfg.window:
            c.append(r[2] or 0.0)
    card = {"ts": now.isoformat(), "strategies": out,
            "regimes": {k: {"n": len(v), "avg_r": round(sum(v) / len(v), 3)} for k, v in regimes.items()}}
    db.set_state(KEY, card)
    return card


def status(db, strategy: str) -> dict:
    return ((db.get_state(KEY) or {}).get("strategies") or {}).get(strategy) or {"state": "active", "mult": 1.0}
