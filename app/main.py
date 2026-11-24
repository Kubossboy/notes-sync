"""ATLAS Trading AI command-line entry point.

Usage:  python -m app.main <command>
  init        create/migrate the database and record a startup event
  env         inspect the host environment (JSON)
  probe       check every free data source / integration and store results
  status      show configuration, safe state, credentials and last probe results
  candles SYM  fetch, validate and store candles (--tf 5m --limit 50)
  quote SYM    cross-checked quote from every source
  collect      candles + quotes for the whole watchlist (or --symbols ...)
  reliability  repeated fetches per source: success rate and latency
  scan         run stock + crypto scanners (--asset stock|crypto|all, --research for delayed data dry-run)
  signals      list recent journaled signals
  halt REASON put the system into SAFE STATE (no new simulated trades)
  resume      clear SAFE STATE (explicit human action, journaled)
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config import PROJECT_ROOT
from app.core.bootstrap import VERSION, bootstrap
from app.core.environment import inspect_environment
from app.integrations.registry import INTEGRATIONS, probe_all


def cmd_status(ctx) -> dict:
    s, db = ctx.settings, ctx.db
    st = ctx.safe_state.status()
    rows = db.conn.execute(
        "SELECT integration, category, status, latency_ms, detail, ts FROM integration_health "
        "WHERE id IN (SELECT MAX(id) FROM integration_health GROUP BY integration) ORDER BY category, integration"
    ).fetchall()
    eq = s.portfolio.starting_equity_usd
    return {
        "version": VERSION,
        "execution_mode": s.execution.mode,
        "provider": s.execution.provider,
        "safe_state": {"halted": st.halted, "reason": st.reason, "since": st.since},
        "portfolio_start_usd": eq,
        "risk": {
            "max_risk_per_trade_usd": round(eq * s.risk.max_risk_per_trade_pct / 100, 2),
            "max_daily_loss_usd": round(eq * s.risk.max_daily_loss_pct / 100, 2),
            "max_open_positions": s.risk.max_open_positions,
            "min_reward_risk": s.risk.min_reward_risk,
            "leverage": s.execution.leverage_enabled,
        },
        "config_hash": s.config_hash(),
        "credentials_present": {i.name: all(ctx.secrets.has(r) for r in i.requires)
                                for i in INTEGRATIONS if i.requires},
        "contact_email_set": ctx.secrets.has("contact_email"),
        "db_tables": len(db.tables()),
        "last_probes": [dict(r) for r in rows],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atlas", description="ATLAS Trading AI (paper trading only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init"); sub.add_parser("env"); sub.add_parser("status")
    pr = sub.add_parser("probe"); pr.add_argument("--only", nargs="*")
    c = sub.add_parser("candles"); c.add_argument("symbol"); c.add_argument("--tf", default="5m")
    c.add_argument("--limit", type=int, default=50)
    q = sub.add_parser("quote"); q.add_argument("symbol")
    co = sub.add_parser("collect"); co.add_argument("--symbols", nargs="*"); co.add_argument("--tf", default="5m")
    rl = sub.add_parser("reliability"); rl.add_argument("--symbols", nargs="*")
    rl.add_argument("--rounds", type=int, default=3); rl.add_argument("--tf", default="5m")
    sc = sub.add_parser("scan"); sc.add_argument("--asset", choices=["stock", "crypto", "all"], default="all")
    sc.add_argument("--symbols", nargs="*"); sc.add_argument("--research", action="store_true")
    sc.add_argument("--json", action="store_true")
    nw = sub.add_parser("news", help="news intelligence: ingest | show SYMBOL | latest | macro | health")
    nw.add_argument("action", choices=["ingest", "show", "latest", "macro", "health"])
    nw.add_argument("symbol", nargs="?")
    nw.add_argument("--limit", type=int, default=15)
    sc.add_argument("--no-news", action="store_true", help="scan without the news engine")
    sg = sub.add_parser("signals"); sg.add_argument("--limit", type=int, default=20)
    h = sub.add_parser("halt"); h.add_argument("reason", nargs="+")
    r = sub.add_parser("resume"); r.add_argument("--by", default="operator"); r.add_argument("--note", default="")
    a = p.parse_args(argv)

    if a.cmd == "env":
        print(json.dumps(inspect_environment(PROJECT_ROOT), indent=2))
        return 0

    ctx = bootstrap(console_log=a.cmd != "status")
    try:
        if a.cmd == "init":
            print(json.dumps({"db": str(ctx.settings.db_path), "tables": ctx.db.tables(),
                              "integrity": ctx.db.integrity_ok()[1]}, indent=2))
        elif a.cmd == "probe":
            res = probe_all(ctx.settings, ctx.secrets, ctx.db, a.only)
            for x in res:
                lat = f"{x.latency_ms:.0f}ms" if x.latency_ms is not None else "-"
                print(f"{x.status:18} {x.integration:26} {x.category:20} {lat:>7}  {x.detail}")
            essential_down = [x for x in res if x.category.startswith("market_data") and x.status != "ok"]
            ok = sum(x.status == "ok" for x in res)
            print(f"\n{ok}/{len(res)} integrations ok")
            return 1 if essential_down and len(essential_down) == sum(
                x.category.startswith("market_data") for x in res) else 0
        elif a.cmd == "scan":
            from app.data.engine import MarketDataEngine
            from app.data.models import AssetClass
            from app.scanners.scanner import Scanner
            eng = MarketDataEngine(ctx.settings, ctx.db, ctx.safe_state, secrets=ctx.secrets)
            news = None
            if not a.no_news and ctx.settings.news.enabled:
                from app.intelligence.engine import NewsEngine
                news = NewsEngine(ctx.settings, ctx.db, ctx.secrets, ctx.safe_state)
            scn = Scanner(ctx.settings, eng, news)
            kinds = [AssetClass.STOCK, AssetClass.CRYPTO] if a.asset == "all" else [AssetClass(a.asset)]
            mode = "research" if a.research else "live"
            for k in kinds:
                syms = [x for x in (a.symbols or []) if (x.upper().endswith("-USD")) == (k == AssetClass.CRYPTO)] or None
                if a.symbols and not syms:
                    continue
                rep = scn.run(k, syms, mode)
                if a.json:
                    print(json.dumps(rep, indent=1, default=str)); continue
                print(f"== {k.value} scan run={rep['run_id']} mode={mode} tf={rep['timeframe']} "
                      f"signals={rep['signals']} halted={rep['safe_state_halted']}")
                for r in rep["symbols"]:
                    if "skipped" in r:
                        print(f"  {r['symbol']:8} skipped: {r['skipped']} [{r.get('data_status')}]")
                        continue
                    ind = r["indicators"]
                    print(f"  {r['symbol']:8} {r['data_status']:8} last={r['last_bar']} close={ind['close']} "
                          f"rsi={ind['rsi14']} trend={r['trend']} liq={r['liquidity'].get('score') and round(r['liquidity']['score'])} "
                          f"{'ALERT ' + r['liquidity_alert'] if r.get('liquidity_alert') else ''}")
                    if r.get("news"):
                        nn = r["news"]
                        agg = None if nn["aggregate"] is None else round(nn["aggregate"], 2)
                        print(f"     news: events={nn['events']} agg={agg} +cat={nn['positive_catalysts']} "
                              f"neg_high_verified={nn['negative_high_verified']} healthy={nn['healthy']} | "
                              f"sentiment={r['sentiment']['composite']} {'; '.join(r['sentiment']['conflicts'])}")
                    for sg_ in r["signals"]:
                        print(f"     SIGNAL {sg_['strategy']:24} {sg_['status']:10} entry={sg_['entry']} stop={sg_['stop']} "
                              f"target={sg_['target']} rr_net={sg_['rr_net']} score={sg_['score']} "
                              f"(on available {sg_['score_on_available']}) conf={sg_['confidence']}"
                              + (f" reason={sg_['reject_reason']}" if sg_['reject_reason'] else ""))
                    for n, why in r["no_setup"].items():
                        print(f"     -      {n:24} {why}")
            st = ctx.safe_state.status()
            print(f"SAFE STATE: {'HALTED - ' + st.reason if st.halted else 'normal'}")
        elif a.cmd == "news":
            from app.intelligence.engine import NewsEngine
            ne = NewsEngine(ctx.settings, ctx.db, ctx.secrets, ctx.safe_state)
            if a.action == "ingest":
                st = ne.ingest()
                for name, v in st["sources"].items():
                    print(f"  {v['status']:18} {name:26} seen={v['seen']:4} new={v['new']:4} {v.get('error', '')}")
                print(f"sources ok={st['ok']} failed={st['failed']} items seen={st['seen']} new={st['new']}")
            elif a.action == "health":
                print(json.dumps(ne.health(), indent=1))
            elif a.action == "show":
                if not a.symbol:
                    print("usage: news show SYMBOL"); return 2
                sym = a.symbol.upper()
                print(json.dumps(ne.symbol_context(sym, "crypto" if sym.endswith("-USD") else "stock"),
                                 indent=1, default=str))
            else:
                where = "WHERE tickers LIKE '%,MACRO,%'" if a.action == "macro" else ""
                rows = ctx.db.conn.execute(
                    f"SELECT published_ts, source, source_tier, verified, sentiment, impact, tickers, title "
                    f"FROM news_events {where} ORDER BY COALESCE(published_ts, fetched_ts) DESC LIMIT ?",
                    (a.limit,)).fetchall()
                for r in rows:
                    print(f"  {r['published_ts'] or '?':25} {r['source']:24} t{r['source_tier']} "
                          f"{'V' if r['verified'] else '-'} s={r['sentiment']:+.2f} {r['impact'] or '-':6} "
                          f"{(r['tickers'] or '').strip(','):20} {r['title'][:90]}")
        elif a.cmd == "signals":
            rows = ctx.db.conn.execute(
                "SELECT s.id, s.ts, s.symbol, s.strategy, s.status, s.entry_price, s.stop_loss, s.take_profit, "
                "s.reward_risk, sc.total_score, sc.score_on_available, s.reject_reason FROM signals s "
                "LEFT JOIN signal_scores sc ON sc.signal_id = s.id ORDER BY s.id DESC LIMIT ?", (a.limit,)).fetchall()
            for r in rows:
                print(dict(r))
        elif a.cmd in ("candles", "quote", "collect", "reliability"):
            from app.data.engine import MarketDataEngine
            eng = MarketDataEngine(ctx.settings, ctx.db, ctx.safe_state, secrets=ctx.secrets)
            wl = a.symbols if getattr(a, "symbols", None) else (
                ctx.settings.markets.stocks.watchlist + ctx.settings.markets.crypto.watchlist)
            if a.cmd == "candles":
                r = eng.get_candles(a.symbol, a.tf, a.limit)
                s = r.series
                print(f"{s.symbol} {s.timeframe} source={s.source} status={s.status.value} "
                      f"tradable={s.tradable} stale={s.stale} market_open={s.market_open} age_s={s.age_seconds}")
                for k in s.candles[-10:]:
                    print(f"  {k.open_time.isoformat()} o={k.open} h={k.high} l={k.low} c={k.close} "
                          f"v={k.volume:.4f} {'COMPLETE' if k.is_complete else 'FORMING'} {k.session or ''}")
                if r.crosscheck:
                    print(f"cross-check: {r.crosscheck.verdict} dev_bps={r.crosscheck.deviations_bps}")
                for w in s.warnings:
                    print(f"warning: {w}")
                for at in r.attempts:
                    if not at.ok:
                        print(f"failed: {at.source} ({at.kind}) {at.error}")
            elif a.cmd == "quote":
                cc, quotes, _ = eng.get_quote_consensus(a.symbol)
                for qq in quotes:
                    sp = f"{qq.spread_bps:.2f}bps" if qq.spread_bps is not None else "-"
                    print(f"  {qq.source:9} {qq.price} bid={qq.bid} ask={qq.ask} spread={sp} "
                          f"{qq.quote_currency} ts={qq.source_ts}")
                print(f"consensus={cc.consensus} verdict={cc.verdict} dev_bps={cc.deviations_bps}")
            elif a.cmd == "collect":
                print(json.dumps(eng.collect(wl, a.tf), indent=1, default=str))
            else:
                from app.data.reliability import reliability_check
                print(json.dumps(reliability_check(eng, wl, a.tf, a.rounds), indent=1))
            st = ctx.safe_state.status()
            print(f"SAFE STATE: {'HALTED - ' + st.reason if st.halted else 'normal'}")
        elif a.cmd == "status":
            print(json.dumps(cmd_status(ctx), indent=2, default=str))
        elif a.cmd == "halt":
            ctx.safe_state.halt(" ".join(a.reason), module="operator")
            print("SAFE STATE: halted")
        elif a.cmd == "resume":
            ctx.safe_state.resume(a.by, a.note)
            print("SAFE STATE: cleared")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
