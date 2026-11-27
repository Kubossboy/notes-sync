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
    tr = sub.add_parser("trade", help="one paper-trading cycle: manage -> scan -> risk -> simulate")
    tr.add_argument("--no-scan", action="store_true", help="only manage open positions + snapshot")
    tr.add_argument("--no-news", action="store_true")
    tr.add_argument("--json", action="store_true")
    sub.add_parser("portfolio", help="equity, cash, P&L, daily loss limit, open positions")
    ps = sub.add_parser("positions"); ps.add_argument("--all", action="store_true"); ps.add_argument("--limit", type=int, default=20)
    od = sub.add_parser("orders"); od.add_argument("--limit", type=int, default=20)
    rk = sub.add_parser("risk", help="recent risk evaluations with failed checks"); rk.add_argument("--limit", type=int, default=10)
    ks = sub.add_parser("kill", help="KILL SWITCH: SAFE STATE (+ optionally flatten all paper positions)")
    ks.add_argument("reason", nargs="+"); ks.add_argument("--flatten", action="store_true")
    bk = sub.add_parser("backtest", help="M6: IS/OOS + walk-forward + sensitivity + cost-stress validation")
    bk.add_argument("--asset", choices=["stock", "crypto", "all"], default="all")
    bk.add_argument("--strategies", nargs="*"); bk.add_argument("--symbols", nargs="*")
    bk.add_argument("--days", type=int); bk.add_argument("--source", help="robustness source, e.g. yahoo (not persisted)")
    bk.add_argument("--no-persist", action="store_true"); bk.add_argument("--json", help="write full results to FILE")
    bk.add_argument("--workers", type=int)
    sub.add_parser("validation", help="latest validation verdict per strategy (feeds the quant score)")
    db_ = sub.add_parser("dashboard", help="local read-only web dashboard (http://127.0.0.1:8765)")
    db_.add_argument("--host", default="127.0.0.1"); db_.add_argument("--port", type=int, default=8765)
    tm = sub.add_parser("trade-manual", help="owner-requested MANUAL paper trade (strategy manual_news); hard risk limits still apply")
    tm.add_argument("symbol"); tm.add_argument("--stop", type=float, required=True); tm.add_argument("--target", type=float, required=True)
    tm.add_argument("--news-id", type=int, required=True, help="news_events.id that motivates the trade")
    tm.add_argument("--note", default=""); tm.add_argument("--dry-run", action="store_true")
    ex = sub.add_parser("export", help="static read-only dashboard snapshot for publishing (scrubbed)")
    ex.add_argument("--out", default="var/site"); ex.add_argument("--no-live-prices", action="store_true")
    sub.add_parser("forwardtest", help="forward paper test stats (news-driven, internal ledger only)")
    lr = sub.add_parser("learn", help="D104: weekly bounded self-tuning | export the learning log")
    lr.add_argument("what", choices=["weekly", "export"]); lr.add_argument("--out", default="var/learning_log.json")
    lr.add_argument("--groups", nargs="*", default=["rsi2_swing", "crypto"])
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
        elif a.cmd in ("trade", "kill"):
            from app.data.engine import MarketDataEngine
            from app.execution.alpaca_paper import AlpacaPaperBroker
            from app.execution.trader import PaperTrader
            from app.scanners.scanner import Scanner
            eng = MarketDataEngine(ctx.settings, ctx.db, ctx.safe_state, secrets=ctx.secrets)
            news = None
            if a.cmd == "trade" and not a.no_news and ctx.settings.news.enabled:
                from app.intelligence.engine import NewsEngine
                news = NewsEngine(ctx.settings, ctx.db, ctx.secrets, ctx.safe_state)
            trader = PaperTrader(ctx.settings, ctx.db, ctx.safe_state, eng, Scanner(ctx.settings, eng, news),
                                 AlpacaPaperBroker(ctx.settings, ctx.secrets))
            if a.cmd == "kill":
                print(json.dumps(trader.kill(" ".join(a.reason), a.flatten), indent=1, default=str))
            else:
                rep = trader.run_once(scan=not a.no_scan)
                if a.json:
                    print(json.dumps(rep, indent=1, default=str))
                else:
                    for m in rep["managed"]:
                        print(f"  manage {m}")
                    for sc_ in rep["scans"]:
                        print(f"  scan {sc_['scanner']} run={sc_['run_id']} signals={sc_['signals']}")
                    for e in rep["executions"]:
                        print(f"  {'EXECUTED' if e['approved'] else 'REJECTED'} {e['symbol']} {e['strategy']} "
                              f"qty={e['qty']} risk=${e['planned_risk_usd']:.2f} rr_net={e['rr_net']} "
                              + (f"fill={e['fill']['fill_price']:.6g}" if e.get('fill') else f"reason={e['reason']}"))
                    if not rep["executions"]:
                        print("  no executable signals (only complete signals with score >= "
                              f"{ctx.settings.scoring.min_score_to_trade} reach the risk engine)")
                    print("  portfolio " + json.dumps(rep["portfolio"]))
            st = ctx.safe_state.status()
            print(f"SAFE STATE: {'HALTED - ' + st.reason if st.halted else 'normal'}")
        elif a.cmd == "portfolio":
            from app.execution.trader import summary
            from app.portfolio.ledger import Ledger
            led = Ledger(ctx.settings, ctx.db)
            st = led.state()
            print(json.dumps(summary(st, ctx.settings), indent=1))
            for p_ in st.open_positions:
                print(f"  OPEN #{p_['id']} {p_['symbol']} qty={p_['quantity']} entry={p_['entry_price']:.6g} "
                      f"mark={p_['mark_price']:.6g} stop={p_['stop_loss']:.6g} target={p_['take_profit']:.6g} "
                      f"unrealized=${p_['unrealized_pnl_usd']:.4f} opened={p_['opened_ts']}")
            print("assumptions: internal simulator; fills at bid/ask (or last ± assumed half-spread) ± slippage; "
                  f"fees crypto {ctx.settings.execution.crypto_fee_bps} bps, stocks {ctx.settings.execution.stock_fee_bps} bps; "
                  "stop gaps filled at bar open; stop assumed before target in the same bar")
        elif a.cmd == "positions":
            where = "" if a.all else "WHERE status='open'"
            for r in ctx.db.conn.execute(f"SELECT id, symbol, strategy, status, quantity, entry_price, exit_price, "
                                         f"stop_loss, take_profit, realized_pnl_usd, unrealized_pnl_usd, r_multiple, "
                                         f"exit_reason, opened_ts, closed_ts FROM positions {where} "
                                         f"ORDER BY id DESC LIMIT ?", (a.limit,)):
                print(dict(r))
        elif a.cmd == "orders":
            for r in ctx.db.conn.execute("SELECT o.id, o.ts, o.client_order_id, o.symbol, o.side, o.purpose, "
                                         "o.order_type, o.quantity, o.status, o.avg_fill_price, f.fee_usd, "
                                         "f.slippage_usd, o.reason FROM order_requests o LEFT JOIN simulated_fills f "
                                         "ON f.order_id=o.id ORDER BY o.id DESC LIMIT ?", (a.limit,)):
                print(dict(r))
        elif a.cmd == "risk":
            for r in ctx.db.conn.execute("SELECT ts, evaluation_id, symbol, approved, quantity, planned_risk_usd, "
                                         "risk_budget_usd, rr_net, equity_usd, reason FROM risk_evaluations "
                                         "ORDER BY id DESC LIMIT ?", (a.limit,)):
                print(dict(r))
        elif a.cmd == "backtest":
            from app.backtest.validation import format_report, run_validation
            from app.data.models import AssetClass
            results = []
            for ac in ([AssetClass.CRYPTO, AssetClass.STOCK] if a.asset == "all" else [AssetClass(a.asset)]):
                out = run_validation(ctx.settings, ctx.db, ac, ctx.secrets, symbols=a.symbols,
                                     workers=a.workers, persist=not a.no_persist, days=a.days,
                                     only=a.strategies, source=a.source)
                results.append(out)
                print(format_report(out))
            if a.json:
                with open(a.json, "w") as fh:
                    json.dump(results, fh, indent=1, default=str)
        elif a.cmd == "dashboard":
            from app.dashboard.server import serve
            srv = serve(ctx.settings, a.host, a.port)
            print(f"ATLAS dashboard (read-only) on http://{a.host}:{a.port}  - Ctrl+C to stop")
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                srv.server_close()
        elif a.cmd == "trade-manual":
            from app.execution.alpaca_paper import AlpacaPaperBroker
            from app.execution.manual import manual_trade
            from app.execution.trader import PaperTrader
            from app.data.engine import MarketDataEngine
            eng = MarketDataEngine(ctx.settings, ctx.db, ctx.safe_state, secrets=ctx.secrets)
            trader = PaperTrader(ctx.settings, ctx.db, ctx.safe_state, eng, None, AlpacaPaperBroker(ctx.settings, ctx.secrets))
            print(json.dumps(manual_trade(trader, a.symbol, a.stop, a.target, a.news_id, a.note, dry_run=a.dry_run),
                             indent=1, default=str))
        elif a.cmd == "export":
            from pathlib import Path
            from app.dashboard.export import export, publish_config
            cfg = publish_config(ctx.settings)
            export(ctx.settings, Path(a.out), cfg["slug"], live_prices=not a.no_live_prices)
            print(json.dumps({"out": a.out, "project": cfg["project"], "path": "/" + cfg["slug"] + "/"}))
        elif a.cmd == "learn":
            if a.what == "weekly":
                from app.learning.tuner import weekly
                rep = weekly(ctx.settings, ctx.db, ctx.secrets, groups=tuple(a.groups))
                print(json.dumps(rep, indent=1, default=str))
            else:
                rows = [dict(r) for r in ctx.db.conn.execute("SELECT ts, kind, strategy, detail FROM learning_events "
                                                             "ORDER BY id")]
                Path(a.out).parent.mkdir(parents=True, exist_ok=True)
                Path(a.out).write_text(json.dumps({"events": rows}, indent=1) + "\n")
                print(json.dumps({"out": a.out, "events": len(rows)}))
        elif a.cmd == "forwardtest":
            from app.quant.engine import forward_test_stats
            v = ctx.settings.validation
            print(json.dumps({"strategies": v.forward_test_strategies, "target_trades": v.forward_test_target_trades,
                              "stats": forward_test_stats(ctx.db)}, indent=1))
            for r in ctx.db.conn.execute("SELECT closed_ts, symbol, strategy, entry_price, exit_price, pnl_usd, "
                                         "r_multiple, exit_reason FROM forward_test_trades ORDER BY id DESC LIMIT 20"):
                print(dict(r))
        elif a.cmd == "validation":
            from app.quant.engine import QuantEngine
            for r in QuantEngine(ctx.settings, ctx.db).summary():
                print(r)
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
