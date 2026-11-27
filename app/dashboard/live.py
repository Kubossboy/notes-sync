"""View model for the public dashboard (static/atlas.html reads data.json). Plain English, no raw codes.

Read-only: uses the same collector as the local dashboard plus the scan_runs journal, so every trade cycle's
scans, signals and rejected candidates (with readable reasons and timestamps) are visible.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.dashboard.data import ReadOnlyDB, _rows, collect
from app.dashboard.plain import exit_reason, explain, sentiment, strategy


def tv_symbol(symbol: str, asset_class: str) -> str:
    """TradingView symbol for the embedded widget (crypto: Binance USDT spot, every pair in the universe is listed)."""
    if asset_class == "crypto" or symbol.endswith("-USD"):
        return "BINANCE:" + symbol.split("-")[0] + "USDT"
    return symbol


def _ts(r: dict) -> str:
    return r.get("finished_ts") or r.get("started_ts") or ""


class _Ctx:
    def __init__(self, settings: Settings):
        sc = settings.scanner
        self.tf = {"stock": sc.stock_timeframe, "crypto": sc.crypto_timeframe}
        t = sc.stock_strategies.get("opening_range_breakout")
        self.orb = int((t.params or {}).get("max_minutes_after_open", 120)) if t else 120

    def why(self, reason, scanner: str) -> str:
        return explain(reason, self.tf.get(scanner, "15m"), self.orb)


def _trend(t):
    return {"up": "Rising", "down": "Falling", "range": "Sideways"}.get(t or "", t or "–")


def _symbol_result(sym: dict, scanner: str, ctx: _Ctx) -> dict:
    sigs = sym.get("signals") or []
    ind = sym.get("indicators") or {}
    out = {"symbol": sym.get("symbol"), "market": "Crypto" if scanner == "crypto" else "Stock",
           "price": ind.get("close"), "rsi": ind.get("rsi14"), "trend": _trend(sym.get("trend")), "rr": None}
    if sym.get("skipped"):
        return out | {"result": "Skipped", "tone": "neutral", "why": ctx.why(sym["skipped"], scanner)}
    if sigs:
        best = max(sigs, key=lambda g: (g.get("status") != "rejected", g.get("rr_net") or -9))
        if best.get("status") == "rejected":
            return out | {"result": "Rejected", "tone": "bad", "strategy": strategy(best.get("strategy")),
                          "rr": best.get("rr_net"), "score": best.get("score"),
                          "why": ctx.why(best.get("reject_reason"), scanner)}
        return out | {"result": "Signal", "tone": "good", "strategy": strategy(best.get("strategy")),
                      "rr": best.get("rr_net"), "score": best.get("score"),
                      "why": "Passed all checks" + (" and was traded." if best.get("status") == "executed" else ".")}
    ns = [(k, v) for k, v in (sym.get("no_setup") or {}).items() if v != "disabled in config"]
    # the most telling reason first: time-window / news-only strategies say little about the chart itself
    weak = ("after open > window", "opening-range", "VERIFIED positive catalyst", "stored book snapshots")
    ns.sort(key=lambda kv: any(w in str(kv[1]) for w in weak))
    return out | {"result": "No setup", "tone": "neutral",
                  "why": ctx.why(ns[0][1], scanner) if ns else "No strategy found a trade."}


def _cycles(runs: list[dict], executed_by_run: dict, ctx: _Ctx, limit: int = 10) -> list[dict]:
    """One row per trade cycle (a stock scan + a crypto scan started within ~2 min), newest first."""
    groups: list[list[dict]] = []
    for r in runs:                                   # runs are newest first
        g = groups[-1] if groups else None
        if g and r["scanner"] not in {x["scanner"] for x in g} and \
                abs((datetime.fromisoformat(g[0]["started_ts"]) - datetime.fromisoformat(r["started_ts"])).total_seconds()) < 120:
            g.append(r)
        else:
            if len(groups) >= limit:
                break
            groups.append([r])
    out = []
    for g in groups:
        rows, counts = [], {"stock": 0, "crypto": 0}
        for r in g:
            for s in json.loads(r["summary_json"] or "{}").get("symbols", []):
                counts[r["scanner"]] = counts.get(r["scanner"], 0) + 1
                rows.append(_symbol_result(s, r["scanner"], ctx) | {"ts": _ts(r)})
        entries = [e for r in g for e in executed_by_run.get(r["id"], [])]
        rejected = [x for x in rows if x["result"] == "Rejected"]
        passed = [x for x in rows if x["result"] == "Signal"]
        best = max(rejected, key=lambda x: x["rr"] if x["rr"] is not None else -9) if rejected else None
        common = Counter(x["why"] for x in rows if x["result"] in ("No setup", "Skipped")).most_common(3)
        parts = []
        if counts.get("stock"):
            parts.append(f"{counts['stock']} stocks")
        if counts.get("crypto"):
            parts.append(f"{counts['crypto']} crypto")
        out.append({
            "ts": max(_ts(r) for r in g), "pairs": len(rows), "what": " + ".join(parts) or "nothing",
            "signals": len(passed) + len(rejected), "entries": len(entries), "passed": passed, "rejected": rejected,
            "entered": entries, "best": best,
            "common": [{"why": w, "n": n} for w, n in common],
            "symbols": [x["symbol"] for x in rows]})
    return out


def learning_view(db) -> dict:
    """D104 'Learning' card: parameter changes (var/params.json), pauses / size changes, lessons, loss causes."""
    from app.learning import params as LP
    from app.learning.review import CAUSE_TEXT
    out = {"changes": [], "events": [], "lessons": [], "causes": [], "weekly": None, "reviewed": 0}
    try:
        doc = LP.load()
        out["changes"] = [{"ts": h["ts"], "text": LP.sentence(h)} for h in reversed(doc["history"][-6:])]
        lr = doc.get("last_run")
        if lr:
            s = "; ".join(f"{'Overnight dip-buy' if g == 'rsi2_swing' else 'crypto'}: {d}" for g, d in lr["summary"].items())
            out["weekly"] = {"ts": lr["ts"], "text": s}
        tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "learning_events" in tables:
            out["events"] = [{"ts": r["ts"], "text": r["detail"]} for r in db.conn.execute(
                "SELECT ts, detail FROM learning_events ORDER BY id DESC LIMIT 6")]
            out["lessons"] = [{"ts": r["ts"], "text": f"{r['symbol']}: {r['lesson']}"} for r in db.conn.execute(
                "SELECT ts, symbol, lesson FROM trade_reviews WHERE lesson IS NOT NULL ORDER BY id DESC LIMIT 3")]
            cnt: dict = {}
            for (c,) in db.conn.execute("SELECT causes FROM trade_reviews WHERE outcome='loss' ORDER BY id DESC LIMIT 50"):
                for t in (c or "").split(","):
                    if t:
                        cnt[t] = cnt.get(t, 0) + 1
            out["causes"] = [{"text": CAUSE_TEXT.get(k, k), "n": v} for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])[:4]]
            out["reviewed"] = db.conn.execute("SELECT COUNT(*) FROM trade_reviews").fetchone()[0]
    except Exception:
        pass
    return out


def llm_view(settings: Settings, regs: dict) -> dict:
    b = regs.get("llm_budget") or {}
    return {"enabled": settings.llm.enabled, "spent_s": b.get("spent"), "budget_s": settings.llm.budget_s,
            "calls": b.get("calls"), "fallbacks": b.get("fallbacks")}


def regime_view(settings: Settings, regs: dict) -> list[dict]:
    """D100: current market regime per asset class + which strategies may trade in it (plain English)."""
    rc = settings.regime
    out = []
    for ac, label in (("stock", "Stocks"), ("crypto", "Crypto")):
        r = regs.get(f"regime_{ac}")
        if not rc.enabled or not r:
            out.append({"market": label, "text": "Not measured yet", "allowed": "", "tone": "neutral"})
            continue
        cell = f"{r['trend']}/{r['vol']}"
        on = [strategy(n) for n, cells in (rc.matrix.get(ac) or {}).items() if cell in cells]
        if ac == "stock" and rc.stock_watch_only:
            allowed = "Watch only: stock signals are logged, no stock trades."
        else:
            allowed = ("Allowed now: " + ", ".join(on)) if on else "No strategy is allowed in this market, so no new trades."
        out.append({"market": label, "benchmark": r.get("benchmark"), "trend": r["trend"], "vol": r["vol"],
                    "text": r.get("text"), "as_of": r.get("as_of"), "allowed": allowed,
                    "tone": {"bull": "good", "bear": "bad"}.get(r["trend"], "neutral")})
    return out


def build_view(settings: Settings, db_path=None, now: datetime | None = None, live_prices: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    ctx = _Ctx(settings)
    d = collect(settings, db_path, now=now, live_prices=live_prices)
    db = ReadOnlyDB(db_path or settings.db_path)
    day_ago = (now - timedelta(hours=24)).isoformat()
    try:
        runs = _rows(db, "SELECT id, started_ts, finished_ts, scanner, summary_json FROM scan_runs "
                         "WHERE mode='live' AND started_ts >= ? ORDER BY id DESC LIMIT 400", (day_ago,))
        closed = _rows(db, "SELECT symbol, asset_class, strategy, direction, entry_price, exit_price, realized_pnl_usd, "
                           "r_multiple, exit_reason, opened_ts, closed_ts, leverage, initial_stop, initial_target "
                           "FROM positions WHERE status='closed' ORDER BY closed_ts DESC LIMIT 25")
        executed = _rows(db, "SELECT ts, symbol, strategy, scan_run_id, entry_price, stop_loss, take_profit FROM signals "
                             "WHERE status='executed' ORDER BY id DESC LIMIT 50")
        eq = db.conn.execute("SELECT ts, equity_usd FROM equity_snapshots ORDER BY id").fetchall()
        learning = learning_view(db)
        whys = {r["symbol"]: json.loads(r["evidence_json"] or "{}").get("why") for r in _rows(
            db, "SELECT p.symbol, s.evidence_json FROM positions p JOIN signals s ON s.id = p.signal_id "
                "WHERE p.status = 'open'", ())}
        regs = {k: json.loads(v) for k, v in db.conn.execute(
            "SELECT key, value_json FROM system_state WHERE key IN ('regime_stock', 'regime_crypto', "
            "'llm_news_summary', 'llm_verdicts', 'llm_budget')").fetchall()}
    finally:
        db.close()
    executed_by_run: dict = {}
    for e in executed:
        executed_by_run.setdefault(e["scan_run_id"], []).append(
            {"symbol": e["symbol"], "strategy": strategy(e["strategy"]), "entry": e["entry_price"],
             "sl": e["stop_loss"], "tp": e["take_profit"], "ts": e["ts"]})

    # newest result per symbol over the last 24 h
    latest: dict[str, dict] = {}
    scanned_24h: dict[str, set] = {"stock": set(), "crypto": set()}
    for r in runs:
        for s in json.loads(r["summary_json"] or "{}").get("symbols", []):
            scanned_24h.setdefault(r["scanner"], set()).add(s.get("symbol"))
            if s.get("symbol") not in latest:
                latest[s.get("symbol")] = _symbol_result(s, r["scanner"], ctx) | {"ts": _ts(r)}
    order = {"Signal": 0, "Rejected": 1, "No setup": 2, "Skipped": 3}
    results = sorted(latest.values(), key=lambda x: (order.get(x["result"], 9), -(x["rr"] or -9), x["symbol"] or ""))
    near = [x for x in results if x["result"] in ("Rejected", "Signal")]
    near.sort(key=lambda x: (x["result"] != "Signal", -(x["rr"] if x["rr"] is not None else -9)))
    best_near = next((x for x in near if x["result"] == "Rejected"), None)

    cycles = _cycles(runs, executed_by_run, ctx)
    o = d["overview"]
    last_scan = max((_ts(r) for r in runs), default=None)
    try:
        age_min = (now - datetime.fromisoformat(last_scan)).total_seconds() / 60 if last_scan else None
    except ValueError:
        age_min = None
    halted = o["safe_state"]["halted"]
    status = "paused" if halted else ("running" if age_min is not None and age_min <= 20 else "not running")
    from app.data.calendar import regular_session_open
    wl_s, wl_c = settings.markets.stocks.watchlist, settings.markets.crypto.watchlist

    step = max(1, len(eq) // 400)
    equity = [[t, round(v, 2)] for t, v in eq[::step]] + ([[now.isoformat(), o["equity"]]] if eq else [])

    positions = [{
        "symbol": p["symbol"], "market": "Crypto" if p["asset_class"] == "crypto" else "Stock", "side": "Long",
        "strategy": strategy(p["strategy"]), "entry": p["entry_price"], "now": p.get("current_price"),
        "sl": p["stop_loss"], "tp": p["take_profit"], "pnl": p.get("unrealized_pnl_usd"), "leverage": p.get("leverage") or 1,
        "opened_ts": p["opened_ts"], "tv": tv_symbol(p["symbol"], p["asset_class"]),
        "why": whys.get(p["symbol"]),
        "overnight": p["strategy"] == settings.swing.strategy} for p in d.get("portfolio", [])]
    trades = [{
        "symbol": c["symbol"], "market": "Crypto" if c["asset_class"] == "crypto" else "Stock", "side": "Long",
        "strategy": strategy(c["strategy"]), "entry": c["entry_price"], "exit": c["exit_price"],
        "sl": c.get("initial_stop"), "tp": c.get("initial_target"), "pnl": c["realized_pnl_usd"],
        "r": c["r_multiple"], "result": exit_reason(c["exit_reason"]),
        "tone": "good" if (c["realized_pnl_usd"] or 0) >= 0 else "bad", "opened_ts": c["opened_ts"],
        "closed_ts": c["closed_ts"]} for c in closed]

    # pipeline: each step = what it does + latest items
    news = d.get("news") or []
    rejected_items = [x for cyc in cycles for x in cyc["rejected"]]
    closed24 = [t for t in trades if (t["closed_ts"] or "") >= day_ago]
    entries24 = [e for e in executed if (e["ts"] or "") >= day_ago]

    def news_item(n):
        label, tone = sentiment(n.get("sentiment"))
        sc = n.get("sentiment")
        return {"ts": n.get("published_ts") or n.get("fetched_ts"), "tone": tone,
                "text": f"{label.capitalize()}" + (f" ({sc:+.2f})" if sc is not None else "") + f": {n.get('title')}"}
    last_c = cycles[0] if cycles else None
    summ = regs.get("llm_news_summary")
    verdicts = [v for v in reversed(regs.get("llm_verdicts") or []) if (v.get("ts") or "") >= day_ago]
    ai_items = [{"ts": v["ts"], "tone": "bad" if v["decision"] == "VETO" else "good",
                 "text": (f"AI check blocked {v['symbol']}: {v['reason']}" if v["decision"] == "VETO"
                          else f"AI check approved {v['symbol']}: {v['reason']}")} for v in verdicts[:5]]
    pipeline = [
        {"key": "scan", "icon": "scan", "label": "Scan", "count": sum(c["pairs"] for c in cycles),
         "desc": "Every ~30 seconds the bot checks the next batch of stocks and crypto for a trade setup.",
         "latest": f"Last: {last_c['what']} checked" if last_c else "No scans yet",
         "items": [{"ts": c["ts"], "tone": "neutral",
                    "text": f"Checked {c['what']}: " + ", ".join(c["symbols"][:8])
                            + (f" and {len(c['symbols']) - 8} more" if len(c["symbols"]) > 8 else "")} for c in cycles]},
        {"key": "filter", "icon": "filter", "label": "Filter", "count": len(rejected_items),
         "desc": "Ideas that look good are still checked for profit after fees, risk and trading volume. Weak ones are dropped.",
         "latest": (f"Last dropped: {rejected_items[0]['symbol']}") if rejected_items else "Nothing dropped recently",
         "items": [{"ts": x["ts"], "tone": "bad", "text": f"{x['symbol']} ({x.get('strategy')}): {x['why']}"}
                   for x in rejected_items[:10]]},
        {"key": "analyst", "icon": "brain", "label": "News analyst (FinBERT)", "count": len(news),
         "desc": "Reads market headlines and rates each one positive, negative or neutral. Good news can support a trade; bad news can block one.",
         "latest": (f"Last: {sentiment(news[0].get('sentiment'))[0]} headline") if news else "No headlines yet",
         "items": ([{"ts": summ["ts"], "tone": "neutral", "text": "AI news summary: " + summ["text"].replace("\n", " ")}]
                   if summ else []) + [news_item(n) for n in news[:10]]},
        {"key": "entry", "icon": "entry", "label": "Entry", "count": len(entries24),
         "desc": "When an idea passes every check, the bot opens a simulated (paper) trade with a stop-loss and a take-profit.",
         "latest": (f"Last: bought {entries24[0]['symbol']}") if entries24 else "No new trades in 24 h",
         "items": ai_items + ([{"ts": e["ts"], "tone": "good",
                     "text": f"Bought {e['symbol']} ({strategy(e['strategy'])}) at {e['entry_price']:,.4g}, "
                             f"stop-loss {e['stop_loss']:,.4g}, take-profit {e['take_profit']:,.4g}"} for e in entries24[:10]]
                   or [{"ts": best_near["ts"] if best_near else None, "tone": "neutral",
                        "text": "No trade opened in the last 24 h. "
                                + (f"Closest call: {best_near['symbol']}. {best_near['why']}" if best_near
                                   else "No idea came close.")}])},
        {"key": "guard", "icon": "guard", "label": "Guard (stop-loss / take-profit)", "count": len(closed24),
         "desc": "Watches open trades and closes them when the price hits the stop-loss (limit the loss) or the take-profit (lock in gain).",
         "latest": (f"Last: {closed24[0]['symbol']} {closed24[0]['result'].lower()}") if closed24 else "No exits in 24 h",
         "items": [{"ts": t["closed_ts"], "tone": t["tone"],
                    "text": f"{t['symbol']}: {t['result'].lower()}, {'+' if (t['pnl'] or 0) >= 0 else '-'}${abs(t['pnl'] or 0):,.2f}"}
                   for t in trades[:10]]},
    ]

    events = [{"ts": c["ts"], "kind": "scan", "text": f"Checked {c['what']}, {c['signals']} ideas, {c['entries']} trades"}
              for c in cycles]
    events += [{"ts": x["ts"], "kind": "filter", "text": f"Dropped {x['symbol']}: {x['why']}"} for x in rejected_items[:15]]
    events += [{"ts": e["ts"], "kind": "entry", "text": f"Bought {e['symbol']} ({strategy(e['strategy'])})"} for e in entries24[:10]]
    events += [{"ts": t["closed_ts"], "kind": "guard" if t["tone"] == "good" else "loss",
                "text": f"Closed {t['symbol']}: {t['result'].lower()}, {'+' if (t['pnl'] or 0) >= 0 else '-'}${abs(t['pnl'] or 0):,.2f}"}
               for t in trades[:10]]
    events = sorted(events, key=lambda e: e["ts"] or "", reverse=True)[:40]

    # live trade charts: open positions, else the last closed trade
    charts = []
    for p in d.get("portfolio", []) or []:
        mark = p.get("current_price") or p["entry_price"]
        charts.append({"symbol": p["symbol"], "asset_class": p["asset_class"], "open": True, "strategy": strategy(p["strategy"]),
                       "entry": p["entry_price"], "sl": p["stop_loss"], "tp": p["take_profit"], "qty": p["quantity"],
                       "leverage": p.get("leverage") or 1, "pnl": p.get("unrealized_pnl_usd"), "opened_ts": p["opened_ts"],
                       "mark": p.get("current_price"),
                       "pnl_offset": (p.get("unrealized_pnl_usd") or 0) - (mark - p["entry_price"]) * p["quantity"],
                       "candles": [[c[0], c[1], c[2], c[3], c[4]] for c in p.get("candles", [])]})
    if not charts and closed:
        c = closed[0]
        cand = []
        if live_prices and c["asset_class"] != "crypto":
            from app.dashboard.charts import fetch_candles
            cand = [[x[0], x[1], x[2], x[3], x[4]] for x in fetch_candles(c["symbol"], c["asset_class"])[0]]
        charts.append({"symbol": c["symbol"], "asset_class": c["asset_class"], "open": False, "strategy": strategy(c["strategy"]),
                       "entry": c["entry_price"], "exit": c["exit_price"], "sl": c.get("initial_stop"), "tp": c.get("initial_target"),
                       "pnl": c["realized_pnl_usd"], "closed_ts": c["closed_ts"], "exit_reason": exit_reason(c["exit_reason"]),
                       "opened_ts": c["opened_ts"], "candles": cand})

    traded = []
    for x in positions + trades:
        if x["symbol"] not in traded:
            traded.append(x["symbol"])
    tv = ([{"label": s, "tv": tv_symbol(s, "crypto" if s.endswith("-USD") else "stock"), "group": "Traded"} for s in traded]
          + [{"label": s, "tv": tv_symbol(s, "crypto"), "group": "Crypto"} for s in wl_c if s not in traded]
          + [{"label": s, "tv": tv_symbol(s, "stock"), "group": "Stocks"} for s in wl_s if s not in traded])
    errors = [{"ts": e["ts"], "where": (e.get("module") or "").split(".")[0], "text": (e.get("message") or "")[:160]}
              for e in d["system"].get("recent_errors", [])[:5]]
    return {
        "generated_ts": now.isoformat(),
        "summary": {"equity": o["equity"], "start_equity": d["start_equity"], "total_pnl": o["total_pnl"],
                    "daily_pnl": o["daily_pnl"], "open_positions": o["open_positions"],
                    "max_open_positions": o["max_open_positions"], "last_scan": last_scan, "status": status,
                    "status_detail": o["safe_state"]["reason"] if halted else "", "halted": halted,
                    "us_market_open": regular_session_open(now), "mode": o["mode"]},
        "pipeline": pipeline, "events": events, "trade_charts": charts, "cycles": cycles,
        "positions": positions, "trades": trades, "near_misses": near[:15], "best_near": best_near,
        "scan_results": results,
        "universe": {"stocks": len(wl_s), "crypto": len(wl_c),
                     "scanned_24h_stocks": len(scanned_24h.get("stock", ())),
                     "scanned_24h_crypto": len(scanned_24h.get("crypto", ())), "scans_24h": len(runs)},
        "equity_curve": equity, "tv_symbols": tv, "errors": errors,
        "regime": regime_view(settings, regs),
        "news_summary": None if not summ else {"ts": summ["ts"], "points": summ["text"].split("\n"),
                                               "model": summ.get("model")},
        "llm": llm_view(settings, regs),
        "learning": learning,
    }
