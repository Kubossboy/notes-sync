"""Local monitoring dashboard (Milestone 7): stdlib HTTP server, server-rendered HTML + JSON API.

Read-only (SQLite mode=ro, no buttons that trade, halt or send anything), bound to 127.0.0.1 by default.
  GET /            HTML dashboard (auto-refresh 30 s)
  GET /api/state   same data as JSON
  GET /healthz     "ok"
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from app.config import Settings
from app.dashboard.data import collect

PRAGUE = ZoneInfo("Europe/Prague")
E = html.escape

CSS = """
body{font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f1419;color:#d8dee6}
header{padding:14px 24px;background:#151c24;border-bottom:1px solid #26313d;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0}h2{font-size:15px;margin:0 0 10px;color:#9fb3c8;text-transform:uppercase;letter-spacing:.06em}
main{padding:18px 24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(560px,1fr));gap:18px}
section{background:#151c24;border:1px solid #26313d;border-radius:8px;padding:14px 16px;overflow-x:auto}
section.wide{grid-column:1/-1}
table{border-collapse:collapse;width:100%;font-size:12.5px}th,td{padding:5px 8px;border-bottom:1px solid #222c37;text-align:left;vertical-align:top}
th{color:#8193a6;font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:#1b242e;border-radius:6px;padding:10px}.kpi b{display:block;font-size:20px;margin-top:2px}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600}
.sim{background:#3b2f0b;color:#f3c969}.real{background:#0d3324;color:#6ee7b7}.bad{background:#3d1416;color:#fca5a5}
.ok{background:#0d3324;color:#6ee7b7}.warn{background:#3b2f0b;color:#f3c969}.muted{color:#718396}
.empty{color:#718396;font-style:italic;padding:6px 0}
"""


def _t(ts: str | None) -> str:
    if not ts:
        return "–"
    try:
        return datetime.fromisoformat(ts).astimezone(PRAGUE).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return E(str(ts))


def _n(x, nd=2, pre="") -> str:
    return "–" if x is None else f"{pre}{x:,.{nd}f}"


def _table(rows: list[dict], cols: list[tuple], empty: str) -> str:
    if not rows:
        return f'<div class="empty">{E(empty)}</div>'
    head = "".join(f"<th>{E(h)}</th>" for h, _ in cols)
    body = "".join("<tr>" + "".join(f.__call__(r) if callable(f) else f"<td>{E(str(r.get(f, '') if r.get(f) is not None else '–'))}</td>"
                                    for _, f in cols) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _status_tag(s: str | None) -> str:
    cls = {"ok": "ok", "validated": "ok", "new": "ok", "executed": "ok", "forward_test": "warn",
           "incomplete": "warn", "insufficient_data": "warn", "needs_credentials": "warn",
           "not_backtestable": "warn", "unavailable": "warn"}.get(s or "", "bad")
    return f'<span class="tag {cls}">{E(s or "–")}</span>'


def render(d: dict) -> str:
    o, sysd, fw = d["overview"], d["system"], d["forward_test"]
    safe = o["safe_state"]
    safe_html = (f'<span class="tag bad">SAFE STATE: {E(safe["reason"] or "")}</span>' if safe["halted"]
                 else '<span class="tag ok">SAFE STATE: normal</span>')
    health_ok = sum(1 for h in sysd["integrations"] if h["status"] == "ok")
    kpi = lambda label, val, extra="": f'<div class="kpi"><span class="muted">{E(label)}</span><b>{val}</b>{extra}</div>'
    overview = "".join([
        kpi("Simulated equity", _n(o["equity"], pre="$")),
        kpi("Daily P&L", _n(o["daily_pnl"], pre="$")),
        kpi("Total P&L", _n(o["total_pnl"], pre="$")),
        kpi("Open positions", f'{o["open_positions"]} / {o["max_open_positions"]}'),
        kpi("Daily loss / limit", f'{_n(o["daily_loss"], pre="$")} / {_n(o["daily_loss_limit"], pre="$")}',
            '<span class="tag bad">LIMIT REACHED</span>' if o["daily_limit_reached"] else ""),
        kpi("Risk per trade", _n(o["risk_per_trade"], pre="$")),
        kpi("System health", f"{health_ok}/{len(sysd['integrations'])} integrations ok"),
        kpi("Losses in a row today", str(o["consecutive_losses_today"]))])
    a = d["analytics"]

    def stats_rows(groups):
        return [{"group": k, **v} for k, v in groups.items()]
    st_cols = [("Group", "group"), ("Trades", lambda r: f'<td class="n">{r["trades"]}</td>'),
               ("Win rate", lambda r: f'<td class="n">{_n(r["win_rate"] * 100 if r["win_rate"] is not None else None, 0)}%</td>'),
               ("Profit factor", lambda r: f'<td class="n">{_n(r["profit_factor"])}</td>'),
               ("Max DD $", lambda r: f'<td class="n">{_n(r["max_drawdown_usd"])}</td>'),
               ("Avg winner $", lambda r: f'<td class="n">{_n(r["avg_winner_usd"])}</td>'),
               ("Avg loser $", lambda r: f'<td class="n">{_n(r["avg_loser_usd"])}</td>'),
               ("Expectancy $", lambda r: f'<td class="n">{_n(r["expectancy_usd"], 3)}</td>'),
               ("Net $", lambda r: f'<td class="n">{_n(r["net_pnl_usd"])}</td>')]
    analytics_rows = ([{"group": "ALL", **a["all"]}] if a["all"]["trades"] else []) + stats_rows(a["by_strategy"]) \
        + stats_rows({f"asset: {k}": v for k, v in a["by_asset_class"].items()})
    fs = fw["stats"]
    sections = [
        ("Overview", '<span class="tag sim">SIMULATED</span>', f'<div class="kpis">{overview}</div>', True),
        ("Market scanner", '<span class="tag real">REAL market data</span> <span class="tag sim">proposals only</span>',
         _table(d["scanner"], [
             ("Time", lambda r: f"<td>{_t(r['ts'])}</td>"), ("Ticker", "symbol"), ("Asset", "asset_class"),
             ("Data ts", lambda r: f"<td>{_t(r['data_ts'])} <span class='muted'>{E(r['data_status'] or '')}</span></td>"),
             ("Score", lambda r: f'<td class="n">{_n(r["score"], 1)}</td>'), ("Strategy", "strategy"),
             ("Entry", lambda r: f'<td class="n">{_n(r["entry"], 4)}</td>'),
             ("Stop", lambda r: f'<td class="n">{_n(r["stop"], 4)}</td>'),
             ("Target", lambda r: f'<td class="n">{_n(r["target"], 4)}</td>'),
             ("R:R net", lambda r: f'<td class="n">{_n(r["rr_net"])}</td>'),
             ("Conf.", lambda r: f'<td class="n">{_n(r["confidence"])}</td>'),
             ("Status", lambda r: f"<td>{_status_tag(r['status'])} {_status_tag(r['validation_status'])}</td>"),
             ("Evidence / reason", lambda r: f"<td class='muted'>{E((r['reject_reason'] or json.dumps(r['missing'] or r['evidence'], default=str))[:220])}</td>")],
             "No journaled signals yet. Live scans run during market hours / via the trading loop."), True),
        ("Portfolio", '<span class="tag sim">SIMULATED</span>', _table(d["portfolio"], [
            ("Symbol", "symbol"), ("Strategy", "strategy"), ("Status", lambda r: f"<td>{_status_tag(r['validation_status'])}</td>"),
            ("Size", lambda r: f'<td class="n">{_n(r["quantity"], 6)}</td>'),
            ("Entry", lambda r: f'<td class="n">{_n(r["entry_price"], 4)}</td>'),
            ("Current", lambda r: f'<td class="n">{_n(r["current_price"], 4)}</td>'),
            ("Unrealized $", lambda r: f'<td class="n">{_n(r["unrealized_pnl_usd"])}</td>'),
            ("Stop", lambda r: f'<td class="n">{_n(r["stop_loss"], 4)}</td>'),
            ("Target", lambda r: f'<td class="n">{_n(r["take_profit"], 4)}</td>'),
            ("Opened", lambda r: f"<td>{_t(r['opened_ts'])}</td>")], "No open positions."), False),
        ("Analytics", '<span class="tag sim">SIMULATED trades</span>',
         _table(analytics_rows, st_cols, "No closed paper trades yet."), False),
        ("Forward paper test", '<span class="tag sim">SIMULATED · internal ledger only · never Alpaca</span>',
         f'<p>Strategies: {E(", ".join(fw["strategies"]))} · closed <b>{fs["closed_trades"]}</b> / target '
         f'{fw["target_trades"]} · open {fs["open_trades"]} · win rate '
         f'{_n(fs["win_rate"] * 100 if fs["win_rate"] is not None else None, 0)}% · PF {_n(fs["profit_factor"])} · '
         f'expectancy {_n(fs["expectancy_r"], 3)} R · net {_n(fs["net_pnl_usd"], pre="$")} · max DD '
         f'{_n(fs["max_drawdown_usd"], pre="$")}</p>' + _table(fw["recent"], [
             ("Closed", lambda r: f"<td>{_t(r['closed_ts'])}</td>"), ("Symbol", "symbol"), ("Strategy", "strategy"),
             ("P&L $", lambda r: f'<td class="n">{_n(r["pnl_usd"])}</td>'),
             ("R", lambda r: f'<td class="n">{_n(r["r_multiple"])}</td>'), ("Exit", "exit_reason")],
             "No forward-test trades yet (needs a verified positive catalyst + price confirmation + score ≥ 70)."), False),
        ("Strategy validation", '<span class="tag real">backtests on REAL history</span>', _table(d["validation"], [
            ("Asset", "asset"), ("Strategy", "strategy"), ("Quant", lambda r: f"<td>{_status_tag(r['quant'])}</td>"),
            ("Verdict", lambda r: f"<td>{_status_tag(r['verdict'])}</td>"),
            ("OOS n", lambda r: f'<td class="n">{r["oos_trades"] if r["oos_trades"] is not None else "–"}</td>'),
            ("OOS PF", lambda r: f'<td class="n">{_n(r["oos_pf"])}</td>'),
            ("OOS exp R", lambda r: f'<td class="n">{_n(r["oos_exp_r"], 3)}</td>'),
            ("Validated", lambda r: f"<td>{_t(r['validated_at'])}</td>"),
            ("Reason", lambda r: f"<td class='muted'>{E(r['reason'][:140])}</td>")], "No backtest runs yet."), True),
        ("News intelligence", '<span class="tag real">REAL news</span>', _table(d["news"], [
            ("Published", lambda r: f"<td>{_t(r['published_ts'] or r['fetched_ts'])}</td>"),
            ("Source", lambda r: f"<td>{E(r['source'])} <span class='muted'>T{r['source_tier']}</span></td>"),
            ("Title", lambda r: f"<td>{E(r['title'][:120])}</td>"),
            ("Tickers", lambda r: f"<td>{E(', '.join(r['tickers'][:4]))}</td>"),
            ("Sentiment", lambda r: f'<td class="n">{_n(r["sentiment"])}</td>'),
            ("Impact", lambda r: f"<td>{E(r['impact'] or '–')} {E(', '.join(c for c in r['catalysts'] if c))}</td>"),
            ("Verified", lambda r: f"<td>{'✔' if r['verified'] else '–'}</td>")], "No news ingested yet."), True),
        ("Risk decisions", '<span class="tag sim">SIMULATED</span>', _table(d["risk"], [
            ("Time", lambda r: f"<td>{_t(r['ts'])}</td>"), ("Symbol", "symbol"),
            ("Approved", lambda r: f"<td>{_status_tag('ok' if r['approved'] else 'rejected')}</td>"),
            ("Risk $", lambda r: f'<td class="n">{_n(r["planned_risk_usd"])}</td>'),
            ("R:R", lambda r: f'<td class="n">{_n(r["rr_net"])}</td>'),
            ("Reason", lambda r: f"<td class='muted'>{E((r['reason'] or '')[:160])}</td>")], "No risk evaluations yet."), False),
        ("System status", '<span class="tag real">REAL probes</span>',
         f'<p>Last market data: {_t(sysd["last_market_data"])} · last news ingest: {_t(sysd["last_news_ingest"])} · '
         f'active modules: {E(", ".join(k for k, v in sysd["active_modules"].items() if v))} · off: '
         f'{E(", ".join(k for k, v in sysd["active_modules"].items() if not v))}</p>'
         + _table(sysd["integrations"], [("Integration", "integration"), ("Category", "category"),
                                         ("Status", lambda r: f"<td>{_status_tag(r['status'])}</td>"),
                                         ("Latency ms", lambda r: f'<td class="n">{_n(r["latency_ms"], 0)}</td>'),
                                         ("Checked", lambda r: f"<td>{_t(r['ts'])}</td>"),
                                         ("Detail", lambda r: f"<td class='muted'>{E((r['detail'] or '')[:90])}</td>")],
                  "No probes yet (run `python -m app.main probe`).")
         + "<h2 style='margin-top:12px'>Missing data (last scans)</h2>"
         + _table(sysd["missing_data"], [("Scanner", "scanner"), ("Symbol", "symbol"), ("Reason", "reason")], "None.")
         + "<h2 style='margin-top:12px'>Recent errors</h2>"
         + _table(sysd["recent_errors"], [("Time", lambda r: f"<td>{_t(r['ts'])}</td>"), ("Module", "module"),
                                         ("Type", "error_type"), ("Message", "message")], "No errors."), True),
    ]
    body = "".join(f'<section class="{"wide" if wide else ""}"><h2>{E(t)} {tag}</h2>{c}</section>'
                   for t, tag, c, wide in sections)
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="30">'
            f'<title>ATLAS Trading AI</title><style>{CSS}</style></head><body><header><h1>ATLAS Trading AI</h1>'
            f'<span class="tag sim">PAPER TRADING ONLY · {E(o["mode"])} · {E(o["provider"])} · Alpaca orders '
            f'{"ON" if o["alpaca_paper_orders_enabled"] else "OFF"}</span>{safe_html}'
            f'<span class="muted">v{E(o["version"])} · updated {_t(d["generated_ts"])} Europe/Prague · '
            f'trading day {E(o["trading_day"])} (New York)</span></header><main>{body}</main></body></html>')


def make_handler(settings: Settings, db_path=None):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body: str, ctype: str):
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path == "/healthz":
                    return self._send(200, "ok", "text/plain")
                if path == "/api/state":
                    return self._send(200, json.dumps(collect(settings, db_path), default=str), "application/json")
                if path == "/":
                    return self._send(200, render(collect(settings, db_path)), "text/html; charset=utf-8")
                return self._send(404, "not found", "text/plain")
            except Exception as e:   # never leak a traceback to the browser
                return self._send(500, f"dashboard error: {type(e).__name__}", "text/plain")

        def log_message(self, *a):
            pass
    return H


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8765, db_path=None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(settings, db_path))
