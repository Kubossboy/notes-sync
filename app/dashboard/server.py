"""Local monitoring dashboard (Milestone 7): stdlib HTTP server, server-rendered HTML + JSON API.

Read-only (SQLite mode=ro, no buttons that trade, halt or send anything), bound to 127.0.0.1 by default.
  GET /            HTML dashboard (top section auto-refreshes every 30 s via /fragment/top)
  GET /fragment/top  cards + open-position charts + balance chart (HTML fragment)
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

JS = """
setInterval(async () => {
  try {
    const r = await fetch('/fragment/top', {cache: 'no-store'});
    if (r.ok) { document.getElementById('top').innerHTML = await r.text();
      document.getElementById('upd').textContent = 'updated ' + new Date().toLocaleTimeString() + ' · auto-refresh 30 s'; }
  } catch (e) {}
}, 30000);
"""

# Static snapshot (Cloudflare Pages): re-fetch the sibling top.html + state.json, show snapshot age, warn when stale.
# LIVE layer: every 5 s the BROWSER polls Coinbase Exchange's public, keyless, CORS-enabled ticker for open crypto
# positions and updates price / unrealized P&L / last candle / equity. Positions themselves still come from the box.
# Stocks: no free keyless CORS source -> snapshot price, labelled. Any fetch failure -> snapshot values, labelled.
LIVE_JS = """
const fmt = (v, d) => v.toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
const usd = v => (v < 0 ? '$-' : '$') + fmt(Math.abs(v), 2);
const cls = (el, v) => { el.classList.remove('pos', 'neg'); if (v > 0) el.classList.add('pos'); else if (v < 0) el.classList.add('neg'); };
const LIVE = {};
function applyLive() {
  let delta = 0, live = 0, total = 0;
  document.querySelectorAll('section.posbox[data-sym]').forEach(sec => {
    total++;
    const d = sec.dataset, qty = +d.qty, entry = +d.entry, mark = +d.mark, fees = +d.fees;
    const tag = sec.querySelector('.lv-tag'), px = LIVE[d.sym];
    if (d.ac !== 'crypto') { tag.className = 'lv-tag tag warn'; tag.textContent = 'snapshot price (no free live stock feed)'; return; }
    if (!px) { tag.className = 'lv-tag tag warn'; tag.textContent = 'snapshot price (live feed unavailable)'; return; }
    live++; delta += qty * (px.price - mark);
    tag.className = 'lv-tag tag ok'; tag.textContent = 'LIVE ' + new Date(px.t).toLocaleTimeString();
    sec.querySelector('.lv-now').textContent = fmt(px.price, 4);
    const u = (px.price - entry) * qty - fees, ue = sec.querySelector('.lv-u');
    ue.querySelector('b').textContent = usd(u); cls(ue, u);
    const svg = sec.querySelector('svg.chart'); if (!svg) return;
    const s = svg.dataset, lo = +s.lo, hi = +s.hi, pt = +s.pt, h = +s.h, pb = +s.pb;
    const y = p => pt + (hi - Math.min(Math.max(p, lo), hi)) / (hi - lo) * (h - pt - pb);
    const w = svg.querySelector('.lcw'), b = svg.querySelector('.lcb'), mk = svg.querySelector('.mk');
    if (w && b) {
      const o = +b.dataset.o, hh = Math.max(+w.dataset.hh, px.price), ll = Math.min(+w.dataset.ll, px.price);
      const col = px.price >= o ? '#34d399' : '#f87171';
      w.setAttribute('y1', y(hh)); w.setAttribute('y2', y(ll)); w.setAttribute('stroke', col);
      const top = y(Math.max(o, px.price)), bot = y(Math.min(o, px.price));
      b.setAttribute('y', top); b.setAttribute('height', Math.max(bot - top, 1)); b.setAttribute('fill', col);
    }
    if (mk) mk.setAttribute('cy', y(px.price));
  });
  [['lv-eq', false], ['lv-tot', true], ['lv-day', true]].forEach(([id, c]) => {
    const el = document.getElementById(id); if (!el) return;
    const v = +el.dataset.v + delta; el.textContent = usd(v); if (c) cls(el, v);
  });
  const ind = document.getElementById('live');
  if (!total) { ind.className = 'tag muted'; ind.textContent = 'no open positions'; }
  else if (live === total) { ind.className = 'tag ok'; ind.textContent = '● LIVE prices (Coinbase, 5 s)'; }
  else if (live) { ind.className = 'tag warn'; ind.textContent = 'LIVE partly - rest snapshot'; }
  else { ind.className = 'tag warn'; ind.textContent = 'SNAPSHOT prices (live feed unavailable)'; }
}
async function pollLive() {
  const syms = [...new Set([...document.querySelectorAll('section.posbox[data-ac="crypto"]')].map(s => s.dataset.sym))];
  await Promise.all(syms.map(async sym => {
    try {
      const r = await fetch('https://api.exchange.coinbase.com/products/' + encodeURIComponent(sym) + '/ticker', {cache: 'no-store'});
      if (!r.ok) throw new Error(r.status);
      const j = await r.json(), p = parseFloat(j.price);
      if (!(p > 0)) throw new Error('bad price');
      LIVE[sym] = {price: p, t: j.time ? Date.parse(j.time) : Date.now()};
    } catch (e) { delete LIVE[sym]; }
  }));
  applyLive();
}
pollLive(); setInterval(pollLive, 5000);
"""

STATIC_JS = """
const upd = document.getElementById('upd');
function age(ts){ const m = Math.round((Date.now() - Date.parse(ts)) / 60000);
  upd.textContent = 'snapshot ' + new Date(ts).toLocaleString() + ' (' + m + ' min ago) · auto-refresh 60 s';
  upd.className = m > 15 ? 'tag bad' : 'muted'; }
age(document.body.dataset.gen);
setInterval(async () => {
  try {
    const s = await fetch('state.json?t=' + Date.now(), {cache: 'no-store'});
    const t = await fetch('top.html?t=' + Date.now(), {cache: 'no-store'});
    if (s.ok && t.ok) { const st = await s.json(); document.getElementById('top').innerHTML = await t.text();
      document.body.dataset.gen = st.generated_ts; if (typeof applyLive === 'function') applyLive(); }
  } catch (e) {}
  age(document.body.dataset.gen);
}, 60000);
"""

CSS = """
*{box-sizing:border-box}body{font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f1419;color:#d8dee6}
header{padding:12px 20px;background:#151c24;border-bottom:1px solid #26313d;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:19px;margin:0;letter-spacing:.04em}h2{font-size:14px;margin:0 0 10px;color:#9fb3c8;text-transform:uppercase;letter-spacing:.05em}
h2.dh{margin:22px 0 8px}h3{font-size:13px;color:#9fb3c8;margin:14px 0 6px}
main{padding:16px 20px;max-width:1500px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin-bottom:14px}
.card{background:#151c24;border:1px solid #26313d;border-radius:10px;padding:14px 16px}
.lbl{color:#8193a6;font-size:12px;text-transform:uppercase;letter-spacing:.05em}.val{font-size:28px;font-weight:650;margin:4px 0}
.sub{color:#8193a6;font-size:12.5px}.pos{color:#34d399}.neg{color:#f87171}
.bar{height:6px;background:#26313d;border-radius:3px;margin:4px 0 6px}.bar i{display:block;height:100%;background:#60a5fa;border-radius:3px}
section{background:#151c24;border:1px solid #26313d;border-radius:10px;padding:14px 16px;margin-bottom:14px;overflow-x:auto}
.posgrid{display:grid;gap:14px;margin-bottom:0}.posgrid.n2{grid-template-columns:1fr 1fr}.posgrid.n1{grid-template-columns:1fr}
@media (max-width:900px){.posgrid.n2{grid-template-columns:1fr}.val{font-size:24px}}
.posline{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;margin-bottom:6px}
svg.chart{width:100%;height:auto;display:block;background:#11171e;border-radius:6px}
details{background:#151c24;border:1px solid #26313d;border-radius:10px;padding:10px 16px;margin-bottom:10px;overflow-x:auto}
summary{cursor:pointer;font-weight:600}details[open] summary{margin-bottom:10px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid #222c37;text-align:left;vertical-align:top}
th{color:#8193a6;font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.sim{background:#3b2f0b;color:#f3c969}.real{background:#0d3324;color:#6ee7b7}.bad{background:#3d1416;color:#fca5a5}
.ok{background:#0d3324;color:#6ee7b7}.warn{background:#3b2f0b;color:#f3c969}.muted{color:#718396}
.empty{color:#8193a6;font-style:italic;padding:10px 0}
.demo{background:#7c2d12;color:#fff;font-weight:700;padding:10px 20px;text-align:center;letter-spacing:.03em}
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


def _cards(d: dict) -> str:
    o, fw = d["overview"], d["forward_test"]
    fs, safe = fw["stats"], o["safe_state"]
    pnl_cls = lambda v: "pos" if (v or 0) > 0 else "neg" if (v or 0) < 0 else ""  # noqa: E731
    risk_ok = not safe["halted"] and not o["daily_limit_reached"]
    risk_txt = ("SAFE STATE: " + (safe["reason"] or "halted")) if safe["halted"] else (
        "Daily limit reached - no new trades" if o["daily_limit_reached"] else "Normal - trading allowed")
    prog = min(100, fs["closed_trades"] / max(fw["target_trades"], 1) * 100)
    card = lambda label, val, sub="", cls="": (f'<div class="card"><div class="lbl">{E(label)}</div>'  # noqa: E731
                                              f'<div class="val {cls}">{val}</div><div class="sub">{sub}</div></div>')
    return '<div class="cards">' + "".join([
        card("Balance (equity)", f'<span id="lv-eq" data-v="{o["equity"]}">{_n(o["equity"], pre="$")}</span>',
             f'cash {_n(o["cash"], pre="$")} · total '
             f'<span id="lv-tot" data-v="{o["total_pnl"]}" class="{pnl_cls(o["total_pnl"])}">{_n(o["total_pnl"], pre="$")}</span>'),
        card("Today P&L", f'<span id="lv-day" data-v="{o["daily_pnl"]}">{_n(o["daily_pnl"], pre="$")}</span>',
             f'trading day {E(o["trading_day"])} (New York)', pnl_cls(o["daily_pnl"])),
        card("Open positions", f'{o["open_positions"]} / {o["max_open_positions"]}',
             f'risk per trade {_n(o["risk_per_trade"], pre="$")}'),
        card("Risk status", '<span class="tag %s">%s</span>' % ("ok" if risk_ok else "bad", "OK" if risk_ok else "STOP"),
             f'{E(risk_txt)}<br>daily loss {_n(o["daily_loss"], pre="$")} / {_n(o["daily_loss_limit"], pre="$")}'),
        card("Forward test (news)", f'{fs["closed_trades"]} / {fw["target_trades"]}',
             f'<div class="bar"><i style="width:{prog:.0f}%"></i></div>net {_n(fs["net_pnl_usd"], pre="$")} · '
             f'win {_n(fs["win_rate"] * 100 if fs["win_rate"] is not None else None, 0)}% · open {fs["open_trades"]}'),
    ]) + "</div>"


def _position_panels(d: dict) -> str:
    from app.dashboard.charts import position_chart_svg
    ps = d["portfolio"]
    if not ps:
        return ('<section><h2>Open positions <span class="tag sim">SIMULATED</span></h2><div class="empty">No open '
                'positions. Charts appear here when ATLAS opens a paper trade (1 position = full width, 2 = side by '
                'side).</div></section>')
    panels = []
    for p in ps:
        u = p["unrealized_pnl_usd"]
        cur = p["current_price"] or p["entry_price"]
        fees = (cur - p["entry_price"]) * p["quantity"] - u          # unrealized = (px - entry) * qty - entry fees
        panels.append(
            f'<section class="posbox" data-sym="{E(p["symbol"])}" data-ac="{E(p["asset_class"])}" data-qty="{p["quantity"]}" '
            f'data-entry="{p["entry_price"]}" data-mark="{cur}" data-fees="{fees:.8g}">'
            f'<h2>{E(p["symbol"])} <span class="tag sim">SIMULATED position</span> '
            f'{_status_tag(p["validation_status"])} <span class="lv-tag"></span></h2>'
            f'<div class="posline"><span>{E(p["strategy"])}</span><span>size {_n(p["quantity"], 6)}</span>'
            f'<span>entry {_n(p["entry_price"], 4)}</span><span>now <b class="lv-now">{_n(p["current_price"], 4)}</b></span>'
            f'<span class="lv-u {"pos" if u > 0 else "neg" if u < 0 else ""}">unrealized <b>{_n(u, pre="$")}</b></span></div>'
            + position_chart_svg(p.get("candles") or [], p["entry_price"], p["stop_loss"], p["take_profit"],
                                 p["current_price"])
            + f'<div class="sub">{E(p.get("chart_tf") or "")} candles · {E(p.get("chart_source") or "none")} '
              f'(REAL prices) · stop {_n(p["stop_loss"], 4)} · target {_n(p["take_profit"], 4)} · opened {_t(p["opened_ts"])}</div>'
              '</section>')
    return f'<div class="posgrid n{min(len(panels), 2)}">{"".join(panels)}</div>'


def _equity_panel(d: dict) -> str:
    from app.dashboard.charts import equity_svg
    return ('<section><h2>Account balance <span class="tag sim">SIMULATED equity</span></h2>'
            + equity_svg(d["equity_curve"], d["start_equity"]) + '</section>')


def render_top(d: dict) -> str:
    return _cards(d) + _position_panels(d) + _equity_panel(d)


def render(d: dict, demo: bool = False, static: bool = False) -> str:
    o, sysd, fw = d["overview"], d["system"], d["forward_test"]
    safe = o["safe_state"]
    safe_html = (f'<span class="tag bad">SAFE STATE: {E(safe["reason"] or "")}</span>' if safe["halted"]
                 else '<span class="tag ok">SAFE STATE: normal</span>')
    a = d["analytics"]

    def stats_rows(groups):
        return [{"group": k, **v} for k, v in groups.items()]
    st_cols = [("Group", "group"), ("Trades", lambda r: f'<td class="n">{r["trades"]}</td>'),
               ("Win rate", lambda r: f'<td class="n">{_n(r["win_rate"] * 100 if r["win_rate"] is not None else None, 0)}%</td>'),
               ("PF", lambda r: f'<td class="n">{_n(r["profit_factor"])}</td>'),
               ("Max DD $", lambda r: f'<td class="n">{_n(r["max_drawdown_usd"])}</td>'),
               ("Avg win $", lambda r: f'<td class="n">{_n(r["avg_winner_usd"])}</td>'),
               ("Avg loss $", lambda r: f'<td class="n">{_n(r["avg_loser_usd"])}</td>'),
               ("Expectancy $", lambda r: f'<td class="n">{_n(r["expectancy_usd"], 3)}</td>'),
               ("Net $", lambda r: f'<td class="n">{_n(r["net_pnl_usd"])}</td>')]
    analytics_rows = ([{"group": "ALL", **a["all"]}] if a["all"]["trades"] else []) + stats_rows(a["by_strategy"]) \
        + stats_rows({f"asset: {k}": v for k, v in a["by_asset_class"].items()})
    health_ok = sum(1 for h in sysd["integrations"] if h["status"] == "ok")
    details = [
        ("Signals (scanner)", f'{len(d["scanner"])} recent', _table(d["scanner"], [
            ("Time", lambda r: f"<td>{_t(r['ts'])}</td>"), ("Ticker", "symbol"), ("Strategy", "strategy"),
            ("Score", lambda r: f'<td class="n">{_n(r["score"], 1)}</td>'),
            ("Entry / stop / target", lambda r: f'<td class="n">{_n(r["entry"], 4)} / {_n(r["stop"], 4)} / {_n(r["target"], 4)}</td>'),
            ("R:R", lambda r: f'<td class="n">{_n(r["rr_net"])}</td>'),
            ("Conf.", lambda r: f'<td class="n">{_n(r["confidence"])}</td>'),
            ("Status", lambda r: f"<td>{_status_tag(r['status'])} {_status_tag(r['validation_status'])}</td>"),
            ("Why", lambda r: f"<td class='muted'>{E((r['reject_reason'] or json.dumps(r['missing'] or r['evidence'], default=str))[:160])}</td>")],
            "No journaled signals yet. Live scans run via the trading loop.")),
        ("Analytics (simulated trades)", f'{a["all"]["trades"]} closed', _table(analytics_rows, st_cols, "No closed paper trades yet.")),
        ("Forward-test trades", f'{fw["stats"]["closed_trades"]} closed', _table(fw["recent"], [
            ("Closed", lambda r: f"<td>{_t(r['closed_ts'])}</td>"), ("Symbol", "symbol"), ("Strategy", "strategy"),
            ("P&L $", lambda r: f'<td class="n">{_n(r["pnl_usd"])}</td>'),
            ("R", lambda r: f'<td class="n">{_n(r["r_multiple"])}</td>'), ("Exit", "exit_reason")],
            "No forward-test trades yet (needs a verified positive catalyst + price confirmation + score >= 70).")),
        ("Strategy validation", "backtests on REAL history", _table(d["validation"], [
            ("Asset", "asset"), ("Strategy", "strategy"), ("Quant", lambda r: f"<td>{_status_tag(r['quant'])}</td>"),
            ("OOS n", lambda r: f'<td class="n">{r["oos_trades"] if r["oos_trades"] is not None else "–"}</td>'),
            ("OOS PF", lambda r: f'<td class="n">{_n(r["oos_pf"])}</td>'),
            ("Reason", lambda r: f"<td class='muted'>{E(r['reason'][:120])}</td>")], "No backtest runs yet.")),
        ("News", f'{len(d["news"])} important items (REAL)', _table(d["news"], [
            ("Published", lambda r: f"<td>{_t(r['published_ts'] or r['fetched_ts'])}</td>"),
            ("Source", "source"), ("Title", lambda r: f"<td>{E(r['title'][:110])}</td>"),
            ("Tickers", lambda r: f"<td>{E(', '.join(r['tickers'][:3]))}</td>"),
            ("Sent.", lambda r: f'<td class="n">{_n(r["sentiment"])}</td>'),
            ("Impact", lambda r: f"<td>{E(r['impact'] or '–')}</td>"),
            ("Verified", lambda r: f"<td>{'✔' if r['verified'] else '–'}</td>")], "No news ingested yet.")),
        ("Risk decisions", f'{len(d["risk"])} recent', _table(d["risk"], [
            ("Time", lambda r: f"<td>{_t(r['ts'])}</td>"), ("Symbol", "symbol"),
            ("Approved", lambda r: f"<td>{_status_tag('ok' if r['approved'] else 'rejected')}</td>"),
            ("Risk $", lambda r: f'<td class="n">{_n(r["planned_risk_usd"])}</td>'),
            ("Reason", lambda r: f"<td class='muted'>{E((r['reason'] or '')[:140])}</td>")], "No risk evaluations yet.")),
        ("System status", f'{health_ok}/{len(sysd["integrations"])} integrations ok · {len(sysd["recent_errors"])} recent errors',
         f'<p>Last market data: {_t(sysd["last_market_data"])} · last news ingest: {_t(sysd["last_news_ingest"])} · '
         f'modules on: {E(", ".join(k for k, v in sysd["active_modules"].items() if v))} · off: '
         f'{E(", ".join(k for k, v in sysd["active_modules"].items() if not v))}</p>'
         + _table(sysd["integrations"], [("Integration", "integration"),
                                         ("Status", lambda r: f"<td>{_status_tag(r['status'])}</td>"),
                                         ("Checked", lambda r: f"<td>{_t(r['ts'])}</td>"),
                                         ("Detail", lambda r: f"<td class='muted'>{E((r['detail'] or '')[:80])}</td>")],
                  "No probes yet (run `python -m app.main probe`).")
         + "<h3>Missing data (last scans)</h3>"
         + _table(sysd["missing_data"], [("Scanner", "scanner"), ("Symbol", "symbol"), ("Reason", "reason")], "None.")
         + "<h3>Recent errors</h3>"
         + _table(sysd["recent_errors"], [("Time", lambda r: f"<td>{_t(r['ts'])}</td>"), ("Module", "module"),
                                         ("Message", "message")], "No errors.")),
    ]
    det = "".join(f'<details><summary>{E(t)} <span class="muted">· {E(sub)}</span></summary>{c}</details>'
                  for t, sub, c in details)
    demo_html = ('<div class="demo">DEMO - throwaway copy of the database with 2 clearly marked DEMO positions built '
                 'from real candles. Not the real ledger.</div>' if demo else "")
    extra = ('<meta name="robots" content="noindex,nofollow,noarchive"><meta name="referrer" content="no-referrer">'
             '<meta name="apple-mobile-web-app-capable" content="yes"><meta name="theme-color" content="#151c24">'
             if static else "")
    snap = ('<div class="demo" style="background:#1e3a5f">READ-ONLY SNAPSHOT of the paper-trading box '
            '(no controls; simulated money only)</div>' if static else "")
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'{extra}<title>ATLAS Trading AI</title><style>{CSS}</style></head>'
            f'<body data-gen="{E(str(d["generated_ts"]))}">{demo_html}{snap}<header><h1>ATLAS</h1>'
            f'<span class="tag sim">PAPER ONLY · Alpaca paper mirror {"ON" if o["alpaca_paper_orders_enabled"] else "OFF"}</span>'
            f'{safe_html}{'<span class="tag warn" id="live">SNAPSHOT prices</span>' if static else ''}<span class="muted" id="upd">updated {_t(d["generated_ts"])} Prague · auto-refresh 30 s</span></header>'
            f'<main><div id="top">{render_top(d)}</div><h2 class="dh">Details</h2>{det}</main>'
            f'<script>{(STATIC_JS + LIVE_JS) if static else JS}</script></body></html>')


def make_handler(settings: Settings, db_path=None, live_prices: bool = True, demo: bool = False):
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
                    return self._send(200, json.dumps(collect(settings, db_path, live_prices=live_prices), default=str),
                                      "application/json")
                if path == "/fragment/top":
                    return self._send(200, render_top(collect(settings, db_path, live_prices=live_prices)),
                                      "text/html; charset=utf-8")
                if path == "/":
                    return self._send(200, render(collect(settings, db_path, live_prices=live_prices), demo),
                                      "text/html; charset=utf-8")
                return self._send(404, "not found", "text/plain")
            except Exception as e:   # never leak a traceback to the browser
                return self._send(500, f"dashboard error: {type(e).__name__}", "text/plain")

        def log_message(self, *a):
            pass
    return H


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8765, db_path=None,
          live_prices: bool = True, demo: bool = False) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("dashboard is localhost-only")
    return ThreadingHTTPServer((host, port), make_handler(settings, db_path, live_prices, demo))
