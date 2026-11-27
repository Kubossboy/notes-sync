"""Compact view model for the public Czech dashboard (static/atlas.html reads data.json).

Read-only: everything comes from the same collector as the local dashboard plus the scan_runs journal,
so every trade cycle's scans, signals and rejected candidates (with reasons and timestamps) are visible.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.dashboard.data import ReadOnlyDB, _rows, collect


def tv_symbol(symbol: str, asset_class: str) -> str:
    """TradingView symbol for the embedded widget (crypto: Binance USDT spot, every pair in the universe is listed)."""
    if asset_class == "crypto" or symbol.endswith("-USD"):
        return "BINANCE:" + symbol.split("-")[0] + "USDT"
    return symbol


def _scan_rows(db, since: str) -> list[dict]:
    return _rows(db, "SELECT id, started_ts, finished_ts, scanner, symbols, summary_json FROM scan_runs "
                     "WHERE mode='live' AND started_ts >= ? ORDER BY id DESC LIMIT 400", (since,))


def _symbol_result(sym: dict) -> dict:
    sigs = sym.get("signals") or []
    ind = sym.get("indicators") or {}
    out = {"symbol": sym.get("symbol"), "price": ind.get("close"), "rsi": ind.get("rsi14"),
           "rvol": ind.get("rvol20"), "trend": sym.get("trend")}
    if sym.get("skipped"):
        return out | {"result": "přeskočeno", "reason": sym["skipped"]}
    if sigs:
        best = max(sigs, key=lambda g: (g.get("status") != "rejected", g.get("score") or 0))
        st = best.get("status") or ""
        res = "zamítnuto" if st == "rejected" else ("signál" if st in ("new", "executed") else st or "signál")
        return out | {"result": res, "strategy": best.get("strategy"), "score": best.get("score"),
                      "rr": best.get("rr_net") or best.get("reward_risk"),
                      "reason": best.get("reject_reason") or best.get("reason") or ""}
    ns = sym.get("no_setup") or {}
    first = next((f"{k}: {v}" for k, v in ns.items() if v != "disabled in config"), "")
    return out | {"result": "bez setupu", "reason": first}


def build_view(settings: Settings, db_path=None, now: datetime | None = None, live_prices: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    d = collect(settings, db_path, now=now, live_prices=live_prices)
    db = ReadOnlyDB(db_path or settings.db_path)
    try:
        runs = _scan_rows(db, (now - timedelta(hours=24)).isoformat())
        closed = _rows(db, "SELECT symbol, asset_class, strategy, direction, entry_price, exit_price, realized_pnl_usd, "
                           "r_multiple, exit_reason, opened_ts, closed_ts, leverage FROM positions WHERE status='closed' "
                           "ORDER BY closed_ts DESC LIMIT 25")
        eq = db.conn.execute("SELECT ts, equity_usd FROM equity_snapshots ORDER BY id").fetchall()
    finally:
        db.close()

    latest: dict[str, dict] = {}       # newest result per symbol over the last 24 h
    scanned_24h: dict[str, set] = {"stock": set(), "crypto": set()}
    log = []
    for r in runs:
        summ = json.loads(r["summary_json"] or "{}")
        syms = summ.get("symbols", [])
        n_sig = sum(len(s.get("signals") or []) for s in syms)
        n_ok = sum(1 for s in syms if not s.get("skipped"))
        if len(log) < 40:
            log.append({"ts": r["finished_ts"] or r["started_ts"], "scanner": r["scanner"],
                        "symbols": len(syms), "analyzed": n_ok, "signals": n_sig})
        for s in syms:
            scanned_24h.setdefault(r["scanner"], set()).add(s.get("symbol"))
            key = s.get("symbol")
            if key not in latest:
                latest[key] = _symbol_result(s) | {"ts": r["finished_ts"] or r["started_ts"], "scanner": r["scanner"]}

    order = {"signál": 0, "zamítnuto": 1, "bez setupu": 2, "přeskočeno": 3}
    results = sorted(latest.values(), key=lambda x: (order.get(x["result"], 9), x["symbol"] or ""))
    near = [x for x in results if x["result"] in ("zamítnuto", "signál")][:25]

    o = d["overview"]
    last_scan = max((r["finished_ts"] or r["started_ts"] for r in runs), default=None)
    try:
        age_min = (now - datetime.fromisoformat(last_scan)).total_seconds() / 60 if last_scan else None
    except ValueError:
        age_min = None
    halted = o["safe_state"]["halted"]
    status = ("ZASTAVENO: " + (o["safe_state"]["reason"] or "safe state")) if halted else \
             ("běží" if age_min is not None and age_min <= 20 else "neběží (žádný sken > 20 min)")
    from app.data.calendar import regular_session_open
    wl_s, wl_c = settings.markets.stocks.watchlist, settings.markets.crypto.watchlist

    step = max(1, len(eq) // 400)
    equity = [[t, round(v, 2)] for t, v in eq[::step]] + ([[now.isoformat(), o["equity"]]] if eq else [])

    positions = [{k: p.get(k) for k in ("symbol", "asset_class", "strategy", "quantity", "entry_price", "current_price",
                                        "unrealized_pnl_usd", "stop_loss", "take_profit", "opened_ts", "leverage",
                                        "margin_usd", "liquidation_price")} | {"tv": tv_symbol(p["symbol"], p["asset_class"])}
                 for p in d.get("portfolio", [])]
    traded = []
    for x in positions + [{"symbol": c["symbol"], "asset_class": c["asset_class"]} for c in closed]:
        if x["symbol"] not in traded:
            traded.append(x["symbol"])
    tv = ([{"label": s, "tv": tv_symbol(s, "crypto" if s.endswith("-USD") else "stock"), "group": "Obchodované"}
           for s in traded]
          + [{"label": s, "tv": tv_symbol(s, "crypto"), "group": "Krypto"} for s in wl_c if s not in traded]
          + [{"label": s, "tv": tv_symbol(s, "stock"), "group": "Akcie"} for s in wl_s if s not in traded])
    return {
        "generated_ts": now.isoformat(),
        "summary": {"equity": o["equity"], "start_equity": d["start_equity"], "total_pnl": o["total_pnl"],
                    "daily_pnl": o["daily_pnl"], "open_positions": o["open_positions"],
                    "max_open_positions": o["max_open_positions"], "last_scan": last_scan, "status": status,
                    "halted": halted, "us_market_open": regular_session_open(now), "mode": o["mode"]},
        "positions": positions,
        "trades": closed,
        "signals": [{k: s.get(k) for k in ("ts", "symbol", "asset_class", "strategy", "score", "rr_net", "status",
                                           "reject_reason", "entry", "stop", "target")} for s in d["scanner"][:25]],
        "near_misses": near,
        "scan_results": results,
        "scan_log": log,
        "universe": {"stocks": len(wl_s), "crypto": len(wl_c),
                     "scanned_24h_stocks": len(scanned_24h.get("stock", ())),
                     "scanned_24h_crypto": len(scanned_24h.get("crypto", ())),
                     "scans_24h": len(runs)},
        "equity_curve": equity,
        "tv_symbols": tv,
        "errors": d["system"].get("recent_errors", [])[:5],
    }
