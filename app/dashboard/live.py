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
                           "r_multiple, exit_reason, opened_ts, closed_ts, leverage, initial_stop, initial_target FROM positions WHERE status='closed' "
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
    day_ago = (now - timedelta(hours=24)).isoformat()
    sigs24 = [x for x in d["scanner"] if (x.get("ts") or "") >= day_ago]
    rejected = [x for x in sigs24 if x.get("status") == "rejected"]
    opened = [x for x in sigs24 if x.get("status") == "executed"]
    closed24 = [c for c in closed if (c.get("closed_ts") or "") >= day_ago]
    news = d.get("news") or []
    last_run = log[0] if log else None

    def mk(r):
        return "krypto" if r["scanner"] == "crypto" else "akcií"
    pipeline = [
        {"key": "scan", "icon": "scan", "label": "Sken", "count": sum(r["symbols"] for r in log),
         "latest": f"{last_run['symbols']} {mk(last_run)}, {last_run['signals']} signálů" if last_run else "zatím nic"},
        {"key": "filter", "icon": "filter", "label": "Filtr", "count": len(rejected),
         "latest": f"{rejected[0]['symbol']}: {rejected[0].get('reject_reason') or ''}" if rejected else "nic nezamítnuto"},
        {"key": "analyst", "icon": "brain", "label": "Analytik · FinBERT", "count": len(news),
         "latest": (news[0]["title"] or "")[:90] + (f" ({news[0]['sentiment']:+.2f})" if news[0].get("sentiment") is not None
                                                   else "") if news else "žádné zprávy"},
        {"key": "entry", "icon": "entry", "label": "Vstup", "count": len(opened),
         "latest": f"{opened[0]['symbol']} long · {opened[0].get('strategy') or ''}" if opened else "žádný nový obchod"},
        {"key": "guard", "icon": "guard", "label": "Strážce SL/TP", "count": len(closed24),
         "latest": (f"{closed24[0]['symbol']} {closed24[0].get('exit_reason')} "
                    f"{(closed24[0].get('realized_pnl_usd') or 0):+.2f} $") if closed24 else "žádný SL/TP zásah"},
    ]
    events = [{"ts": r["ts"], "kind": "scan", "text": f"Sken {r['symbols']} {mk(r)} · {r['signals']} signálů"} for r in log[:15]]
    events += [{"ts": x["ts"], "kind": "filter", "text": f"Filtr vyřadil {x['symbol']}: {x.get('reject_reason') or ''}"}
               for x in rejected[:20]]
    events += [{"ts": x["ts"], "kind": "entry", "text": f"Vstup otevřel {x['symbol']} long ({x.get('strategy')})"}
               for x in opened[:10]]
    events += [{"ts": c["closed_ts"], "kind": "guard" if (c.get("realized_pnl_usd") or 0) >= 0 else "loss",
                "text": f"Strážce zavřel {c['symbol']}: {c.get('exit_reason')} {(c.get('realized_pnl_usd') or 0):+.2f} $"}
               for c in closed[:10]]
    events = sorted(events, key=lambda e: e["ts"] or "", reverse=True)[:40]

    # live trade charts: open positions, else the last closed trade (entry / SL / TP lines; candles for stocks
    # come from this snapshot, crypto candles are refreshed client-side from Bybit public klines)
    charts = []
    src = d.get("portfolio", []) or []
    for p in src:
        charts.append({"symbol": p["symbol"], "asset_class": p["asset_class"], "open": True, "strategy": p["strategy"],
                       "entry": p["entry_price"], "sl": p["stop_loss"], "tp": p["take_profit"], "qty": p["quantity"],
                       "leverage": p.get("leverage") or 1, "pnl": p.get("unrealized_pnl_usd"), "opened_ts": p["opened_ts"],
                       "mark": p.get("current_price"),
                       "pnl_offset": (p.get("unrealized_pnl_usd") or 0) - ((p.get("current_price") or p["entry_price"])
                                                                            - p["entry_price"]) * p["quantity"],
                       "candles": [[c[0], c[1], c[2], c[3], c[4]] for c in p.get("candles", [])]})
    if not charts and closed:
        c = closed[0]
        cand = []
        if live_prices and c["asset_class"] != "crypto":
            from app.dashboard.charts import fetch_candles
            cand = [[x[0], x[1], x[2], x[3], x[4]] for x in fetch_candles(c["symbol"], c["asset_class"])[0]]
        charts.append({"symbol": c["symbol"], "asset_class": c["asset_class"], "open": False, "strategy": c["strategy"],
                       "entry": c["entry_price"], "exit": c["exit_price"], "sl": c.get("initial_stop"), "tp": c.get("initial_target"),
                       "pnl": c["realized_pnl_usd"], "closed_ts": c["closed_ts"], "exit_reason": c["exit_reason"],
                       "opened_ts": c["opened_ts"], "candles": cand})
    return {
        "generated_ts": now.isoformat(),
        "pipeline": pipeline,
        "events": events,
        "trade_charts": charts,
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
