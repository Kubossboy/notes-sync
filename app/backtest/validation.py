"""VALIDATION - in-sample / out-of-sample, walk-forward, sensitivity and cost stress per strategy.

Protocol (fixed BEFORE looking at results, DECISIONS D64-D70):
  * period split by TIME: first (1 - oos_fraction) in-sample, last oos_fraction out-of-sample.
  * walk-forward: N equal time blocks; fold f trains on blocks < f (expanding window), picks the
    variant (default / loose / strict) with the best training expectancy (>= wf_min_train_trades,
    else default) and is tested on block f. Selected params are REPORTED, never deployed.
  * sensitivity: the loose (x0.8) and strict (x1.2) neighbours on the out-of-sample period.
  * cost stress: default params re-run with fees/slippage/spread x stress_cost_multiplier.
  * verdict uses the DEFAULT (live) parameters only:
        validated          OOS trades >= min, PF >= min_pf, expectancy > 0, max DD < max,
                           WF positive folds >= fraction, stress PF > 1, neighbours PF > 1
        insufficient_data  too few OOS trades to judge (quant unavailable -> signal incomplete)
        failed             otherwise (quant marks the signal rejected)
        not_backtestable   needs data that has no free historical archive
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict

import pandas as pd

from app.backtest.engine import NOT_BACKTESTABLE, Backtester, Candidate, Trade, metrics, variants_for
from app.backtest.history import load
from app.config import Settings
from app.data.models import AssetClass, tf_seconds
from app.database.db import Database, utcnow

SOURCES = {AssetClass.CRYPTO: ("coinbase", "15m"), AssetClass.STOCK: ("alpaca", "5m")}


def params_hash(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


def strategy_params(settings: Settings, ac: AssetClass) -> dict[str, dict]:
    from app.strategies.crypto import CRYPTO_STRATEGIES
    from app.strategies.stock import STOCK_STRATEGIES
    table = STOCK_STRATEGIES if ac == AssetClass.STOCK else CRYPTO_STRATEGIES
    toggles = settings.scanner.stock_strategies if ac == AssetClass.STOCK else settings.scanner.crypto_strategies
    out = {}
    for name in table:
        t = toggles.get(name)
        out[name] = dict(t.params) if t else {}
    return out


DIAG_MIN_RR = 1.0


def diag_settings(settings: Settings) -> Settings:
    """Settings with the live net R:R gate lowered to 1.0 - DIAGNOSTIC ONLY (is there any edge at all?)."""
    return settings.model_copy(update={"risk": settings.risk.model_copy(update={"min_reward_risk": DIAG_MIN_RR})})


def _gen(job):
    """Worker: candidates for one symbol (all strategies x variants) + stressed-cost default candidates."""
    settings, ac, source, symbol, tf, days, strategies, stress_mult, secrets = job
    raw = load(source, symbol, tf, days, secrets)
    bt = Backtester(settings)
    ind = bt.prepare(raw, ac)
    cands = bt.candidates(ind, symbol, ac, tf, strategies, diag_min_rr=DIAG_MIN_RR)
    sbt = Backtester(settings, cost_multiplier=stress_mult)
    stressed = sbt.candidates(ind, symbol, ac, tf, {k: {"default": v["default"]} for k, v in strategies.items()})
    return symbol, cands, stressed, {f"{k[0]}|{k[1]}": dict(v) for k, v in bt.diag.items()}


def _seg(cands, lo, hi):
    return [c for c in cands if (lo is None or c.signal_ts >= lo) and (hi is None or c.signal_ts < hi)]


def evaluate_strategy(settings: Settings, bt: Backtester, sbt: Backtester, data: dict, ac: AssetClass, tf: str,
                      name: str, cands: list[Candidate], stressed: list[Candidate],
                      t0: pd.Timestamp, t1: pd.Timestamp, diag: dict | None = None,
                      dbt: Backtester | None = None) -> tuple[dict, dict[str, list[Trade]]]:
    v = settings.validation
    all_cands = cands
    cands = [c for c in cands if c.gated]
    stressed = [c for c in stressed if c.gated]
    tf_s = tf_seconds(tf)
    ppy = 365 if ac == AssetClass.CRYPTO else 252
    split = t0 + (t1 - t0) * (1 - v.oos_fraction)
    by_var: dict[str, list[Candidate]] = {}
    for c in cands:
        by_var.setdefault(c.variant, []).append(c)
    run = lambda b, cs: b.simulate(data, cs, ac, tf_s)
    M = lambda tr: metrics(tr, settings.portfolio.starting_equity_usd, ppy)
    trades: dict[str, list[Trade]] = {}
    res: dict = {"split_ts": split.isoformat(), "raw_signals": {k: len(x) for k, x in by_var.items()}}
    d = by_var.get("default", [])
    trades["in_sample"] = run(bt, _seg(d, None, split))
    trades["out_of_sample"] = run(bt, _seg(d, split, None))
    res["in_sample"], res["out_of_sample"] = M(trades["in_sample"]), M(trades["out_of_sample"])
    res["full_period"] = M(run(bt, d))
    oos = trades["out_of_sample"]
    res["oos_by_regime"] = {}
    for reg in ("up", "down", "range"):
        sel = [t for t in oos if _trend_at(data[t.symbol], t.signal_ts) == reg]
        res["oos_by_regime"][reg] = {"trades": len(sel), "net_pnl_usd": round(sum(t.pnl for t in sel), 4),
                                     "expectancy_r": round(sum(t.r_multiple for t in sel) / len(sel), 4) if sel else None}
    res["oos_by_symbol"] = {s: {"trades": len(x), "net_pnl_usd": round(sum(t.pnl for t in x), 4)}
                            for s in sorted({t.symbol for t in oos}) for x in [[t for t in oos if t.symbol == s]]}
    # sensitivity
    res["sensitivity_oos"] = {vn: M(run(bt, _seg(by_var.get(vn, []), split, None))) for vn in ("loose", "strict")}
    # cost stress
    trades["stress_oos"] = run(sbt, _seg(stressed, split, None))
    res["stress_oos"] = M(trades["stress_oos"]) | {"cost_multiplier": v.stress_cost_multiplier}
    # walk-forward
    edges = [t0 + (t1 - t0) * k / v.walk_forward_blocks for k in range(v.walk_forward_blocks + 1)]
    folds, wf_trades = [], []
    for f in range(1, v.walk_forward_blocks):
        best, best_e = "default", None
        for vn, cs in by_var.items():
            m = M(run(bt, _seg(cs, None, edges[f])))
            if m["trades"] >= v.wf_min_train_trades and (best_e is None or m["expectancy_r"] > best_e):
                best, best_e = vn, m["expectancy_r"]
        hi = edges[f + 1] if f + 1 < len(edges) - 1 else None
        tt = run(bt, _seg(by_var.get(best, []), edges[f], hi))
        trades[f"wf_fold_{f}"] = tt
        wf_trades += tt
        folds.append({"fold": f, "test_start": edges[f].isoformat(), "selected_variant": best,
                      "train_expectancy_r": best_e, "test": M(tt)})
    with_tr = [x for x in folds if x["test"]["trades"] > 0]
    pos = [x for x in with_tr if x["test"]["net_pnl_usd"] > 0]
    res["walk_forward"] = {"folds": folds, "combined_oos": M(wf_trades),
                           "folds_with_trades": len(with_tr), "positive_folds": len(pos),
                           "positive_fraction": round(len(pos) / len(with_tr), 3) if with_tr else None}
    if diag is not None:
        res["setup_diagnostics"] = diag
    if dbt is not None:   # diagnostic: live net R:R gate lowered to 1.0 (NOT used for the verdict)
        dd = [c for c in all_cands if c.variant == "default"]
        res["diagnostic_rr_gate_1"] = {"note": "net R:R gate lowered from %.1f to %.1f - diagnostic only, never "
                                               "used for verdicts or trading" % (settings.risk.min_reward_risk,
                                                                                 DIAG_MIN_RR),
                                       "in_sample": M(run(dbt, _seg(dd, None, split))),
                                       "out_of_sample": M(run(dbt, _seg(dd, split, None)))}
    res["verdict"], res["reasons"] = verdict(settings, res)
    return res, trades


def _trend_at(df: pd.DataFrame, ts) -> str | None:
    p = df.index.searchsorted(ts, side="right") - 1
    t = df["trend"].iloc[p] if p >= 0 else None
    return t if isinstance(t, str) else None


def verdict(settings: Settings, res: dict) -> tuple[str, list[str]]:
    v = settings.validation
    o = res["out_of_sample"]
    if o["trades"] < v.min_oos_trades:
        why = [f"only {o['trades']} out-of-sample trades (< {v.min_oos_trades}) - cannot distinguish edge from noise"]
        dx = (res.get("diagnostic_rr_gate_1") or {}).get("out_of_sample") or {}
        if dx.get("trades", 0) >= v.min_oos_trades:
            pf = dx.get("profit_factor")
            why.append(f"diagnostic (net R:R gate {DIAG_MIN_RR}): {dx['trades']} OOS trades, PF {pf}, expectancy "
                       f"{dx.get('expectancy_r')} R - " + ("no evidence of an edge after costs"
                                                           if (pf or 0) < 1 else "not used for the verdict"))
        return "insufficient_data", why
    fails = []
    pf = o.get("profit_factor")
    if pf is None or pf < v.min_profit_factor:
        fails.append(f"OOS profit factor {pf} < {v.min_profit_factor}")
    if o["expectancy_r"] <= 0:
        fails.append(f"OOS expectancy {o['expectancy_r']} R <= 0 after costs")
    if o["max_drawdown_pct"] >= v.max_drawdown_pct:
        fails.append(f"OOS max drawdown {o['max_drawdown_pct']}% >= {v.max_drawdown_pct}%")
    wf = res["walk_forward"]
    if wf["folds_with_trades"] < 3 or (wf["positive_fraction"] or 0) < v.min_wf_positive_fraction:
        fails.append(f"walk-forward positive folds {wf['positive_folds']}/{wf['folds_with_trades']} "
                     f"(< {v.min_wf_positive_fraction:.0%} or < 3 folds with trades)")
    s = res["stress_oos"]
    if s["trades"] == 0 or (s.get("profit_factor") or 0) <= 1:
        fails.append(f"cost stress x{v.stress_cost_multiplier}: PF {s.get('profit_factor')} <= 1")
    for vn, m in res["sensitivity_oos"].items():
        if m["trades"] >= 10 and (m.get("profit_factor") or 0) <= 1:
            fails.append(f"parameter sensitivity: {vn} variant OOS PF {m.get('profit_factor')} <= 1 (fragile)")
    return ("failed", fails) if fails else ("validated", ["all out-of-sample, walk-forward, stress and "
                                                        "sensitivity gates passed"])


def run_validation(settings: Settings, db: Database, ac: AssetClass, secrets=None, symbols: list[str] | None = None,
                   workers: int | None = None, persist: bool = True, days: int | None = None,
                   only: list[str] | None = None, progress=print, source: str | None = None) -> dict:
    """source=None -> the primary source (verdicts persisted). Another source (e.g. 'yahoo', 60 days,
    consolidated volume) is a robustness cross-check only and is never persisted as a verdict."""
    from app.core.bootstrap import VERSION
    src0, tf = SOURCES[ac]
    if source and source != src0:
        persist = False
    source = source or src0
    v = settings.validation
    days = days or (v.history_days_stock if ac == AssetClass.STOCK else v.history_days_crypto)
    symbols = symbols or (settings.markets.stocks.watchlist if ac == AssetClass.STOCK
                          else settings.markets.crypto.watchlist)
    params = strategy_params(settings, ac)
    names = [n for n in params if n not in NOT_BACKTESTABLE and (only is None or n in only)]
    strategies = {n: variants_for(params[n]) for n in names}
    jobs = [(settings, ac, source, s, tf, days, strategies, v.stress_cost_multiplier, secrets) for s in symbols]
    workers = workers or max(1, min(len(jobs), (os.cpu_count() or 2) - 2))
    cands: list[Candidate] = []
    stressed: list[Candidate] = []
    diag: dict[str, dict] = {}
    with ProcessPoolExecutor(workers) as ex:
        for sym, c, sc, dg in ex.map(_gen, jobs):
            cands += c
            stressed += sc
            for k, cnt in dg.items():
                tgt = diag.setdefault(k, {})
                for kk, n in cnt.items():
                    tgt[kk] = tgt.get(kk, 0) + n
            progress(f"  {sym}: {sum(x.gated for x in c)} gated candidate signals ({len(c)} incl. diagnostic)")
    bt, sbt = Backtester(settings), Backtester(settings, v.stress_cost_multiplier)
    dbt = Backtester(diag_settings(settings))
    data = {s: bt.prepare(load(source, s, tf, days, secrets), ac) for s in symbols}
    t0 = min(df.index[0] for df in data.values())
    t1 = max(df.index[-1] for df in data.values())
    batch = uuid.uuid4().hex[:12]
    cost_model = {k: getattr(settings.execution, k) for k in (
        "crypto_fee_bps", "stock_fee_bps", "slippage_bps_crypto", "slippage_bps_stock",
        "assumed_half_spread_bps_crypto", "assumed_half_spread_bps_stock", "commission_per_trade_usd")}
    out = {"batch_id": batch, "asset_class": ac.value, "source": source, "timeframe": tf, "symbols": symbols,
           "period": [t0.isoformat(), t1.isoformat()], "strategies": {}}
    for name in [n for n in params if only is None or n in only]:
        if name in NOT_BACKTESTABLE:
            res, trades = {"reason": NOT_BACKTESTABLE[name]}, {}
            res["verdict"], res["reasons"] = "not_backtestable", [NOT_BACKTESTABLE[name]]
        else:
            res, trades = evaluate_strategy(settings, bt, sbt, data, ac, tf, name,
                                            [c for c in cands if c.strategy == name],
                                            [c for c in stressed if c.strategy == name], t0, t1,
                                            {k.split("|")[1]: x for k, x in diag.items() if k.split("|")[0] == name},
                                            dbt)
        out["strategies"][name] = res
        progress(f"  {name}: {res['verdict']}")
        if persist:
            _persist(db, batch, ac, name, source, tf, symbols, t0, t1, res, trades, params[name],
                     cost_model, settings.config_hash(), VERSION)
    return out


def _persist(db, batch, ac, name, source, tf, symbols, t0, t1, res, trades, params, cost_model, chash, version):
    metrics_blob = {k: v for k, v in res.items() if k not in ("verdict", "reasons")}
    with db.transaction():
        rid = db.insert("backtest_runs", {
            "ts": utcnow(), "batch_id": batch, "asset_class": ac.value, "strategy": name, "source": source,
            "timeframe": tf, "symbols": ",".join(symbols), "period_start": t0.isoformat(),
            "period_end": t1.isoformat(), "oos_start": res.get("split_ts"),
            "params_json": json.dumps(params, sort_keys=True), "params_hash": params_hash(params),
            "cost_model_json": json.dumps(cost_model, sort_keys=True), "config_hash": chash,
            "app_version": version, "metrics_json": json.dumps(metrics_blob, default=str),
            "verdict": res["verdict"], "reasons_json": json.dumps(res["reasons"])})
        for seg, tr in trades.items():
            db.conn.executemany(
                "INSERT INTO backtest_trades(run_id, segment, variant, symbol, signal_ts, entry_ts, exit_ts, "
                "entry_price, exit_price, stop_loss, take_profit, quantity, fees_usd, pnl_usd, r_multiple, "
                "exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(rid, seg, t.variant, t.symbol, t.signal_ts.isoformat(), t.entry_ts.isoformat(),
                  t.exit_ts.isoformat(), t.entry_price, t.exit_price, t.stop, t.target, t.qty, t.fees, t.pnl,
                  t.r_multiple, t.exit_reason) for t in tr])
    return rid


def format_report(out: dict) -> str:
    def f(x, nd=2):
        return "-" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))
    lines = [f"{out['asset_class']} | {out['source']} {out['timeframe']} | {', '.join(out['symbols'])} | "
             f"{out['period'][0][:10]} -> {out['period'][1][:10]}",
             f"{'strategy':22} {'IS n':>5} {'IS PF':>6} | {'OOS n':>5} {'win%':>5} {'PF':>5} {'exp R':>6} "
             f"{'net %':>7} {'maxDD%':>6} | {'WF n':>4} {'WF PF':>5} {'WF+':>5} | {'strs PF':>7} | verdict"]
    for n, r in out["strategies"].items():
        if "out_of_sample" not in r:
            lines.append(f"{n:22} {'':>5} {'':>6} | not backtestable: {r['reasons'][0]}")
            continue
        i, o, w, st = r["in_sample"], r["out_of_sample"], r["walk_forward"], r["stress_oos"]
        lines.append(
            f"{n:22} {i['trades']:>5} {f(i.get('profit_factor')):>6} | {o['trades']:>5} "
            f"{f(o.get('win_rate', 0) * 100 if o['trades'] else None, 0):>5} {f(o.get('profit_factor')):>5} "
            f"{f(o.get('expectancy_r'), 3):>6} {f(o.get('net_return_pct')):>7} {f(o.get('max_drawdown_pct')):>6} | "
            f"{w['combined_oos']['trades']:>4} {f(w['combined_oos'].get('profit_factor')):>5} "
            f"{str(w['positive_folds']) + '/' + str(w['folds_with_trades']):>5} | {f(st.get('profit_factor')):>7} | "
            f"{r['verdict']}")
        dg = r.get("setup_diagnostics", {}).get("default")
        if dg:
            rej = sorted(((k[10:], v) for k, v in dg.items() if k.startswith("rejected: ")), key=lambda x: -x[1])
            lines.append(f"{'':22}   setups {dg.get('raw_setups', 0)}, plan ok {dg.get('plan_ok', 0)}; "
                         + "; ".join(f"{k} {v}" for k, v in rej[:3]))
        dx = r.get("diagnostic_rr_gate_1", {}).get("out_of_sample")
        if dx and dx.get("trades"):
            lines.append(f"{'':22}   [diagnostic R:R>=1 gate] OOS n {dx['trades']} PF {f(dx.get('profit_factor'))} "
                         f"exp R {f(dx.get('expectancy_r'), 3)} net % {f(dx.get('net_return_pct'))}")
    return "\n".join(lines)
