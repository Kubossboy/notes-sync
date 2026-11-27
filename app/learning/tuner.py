"""D104 weekly self-tuning (separate scheduled workflow). Paper research only; nothing here trades.

For each group (overnight dip-buy, crypto D99 set) it re-runs the walk-forward backtest for the CURRENT parameters
and for every single one-step neighbour in the bounded grid (app/learning/params.py), regime-gated like live, after
costs (live fee/slippage model). A neighbour is adopted only if it is better in-sample AND beats the current values
out-of-sample by >= min_gain_r with >= min trades and a positive out-of-sample expectancy. Max one change per group
per week. Before tuning, a past change whose live trades degraded is reverted. Hard rails are never in the grid."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.config import PROJECT_ROOT
from app.learning import params as LP

# ------------------------------------------------------------------ shared
def stats(rs: list[float]) -> dict:
    if not rs:
        return {"n": 0, "avg_r": 0.0, "win": 0.0}
    a = np.array(rs, dtype=float)
    return {"n": len(a), "avg_r": round(float(a.mean()), 4), "win": round(float((a > 0).mean()), 3)}


def portfolio(ts: list[dict], max_open: int = 2) -> list[dict]:
    ts = sorted(ts, key=lambda t: t["in"])
    open_, out = [], []
    for t in ts:
        open_ = [o for o in open_ if o["out"] > t["in"]]
        if len(open_) >= max_open or any(o["sym"] == t["sym"] for o in open_):
            continue
        open_.append(t)
        out.append(t)
    return out


def decide(cur: dict, cands: dict, min_n: int, min_gain: float) -> tuple[str | None, float | None, str]:
    """cur/cands: {"is": stats, "oos": stats}. Pick the best in-sample neighbour, then demand OOS proof."""
    if not cands:
        return None, None, "no neighbouring values to test"
    (key, val), best = max(cands.items(), key=lambda kv: kv[1]["is"]["avg_r"])
    i, o = best["is"], best["oos"]
    if i["avg_r"] <= cur["is"]["avg_r"]:
        return None, None, "no neighbour beat the current values in-sample"
    if o["n"] < min_n:
        return None, None, f"best candidate has only {o['n']} out-of-sample trades (needs {min_n})"
    if o["avg_r"] < cur["oos"]["avg_r"] + min_gain or o["avg_r"] <= 0:
        return None, None, (f"best candidate did not beat the current values out-of-sample "
                            f"({o['avg_r']:+.3f} R vs {cur['oos']['avg_r']:+.3f} R)")
    return key, val, (f"it did better out-of-sample after costs ({o['avg_r']:+.3f} R vs {cur['oos']['avg_r']:+.3f} R "
                      f"per trade over {o['n']} trades)")


# ------------------------------------------------------------------ overnight dip-buy (daily stocks)
def rsi2_trades(df: pd.DataFrame, sym: str, rsi_max: float, k: float, hold: int, slip: float, reg) -> list[dict]:
    from app.strategies.swing import atr, rsi
    c = df["close"]; r2 = rsi(c, 2); s200 = c.rolling(200).mean(); s5 = c.rolling(5).mean(); a = atr(df, 10)
    o, lo = df["open"].to_numpy(), df["low"].to_numpy()
    out, i, n = [], 200, len(df)
    while i < n - 1:
        if c.iloc[i] > s200.iloc[i] and r2.iloc[i] < rsi_max and reg(df.index[i]):
            e = c.iloc[i]; st = e - k * a.iloc[i]; tp = e + 2.2 * (e - st); j = i + 1; x = None
            while j < n:
                if o[j] <= st: x = o[j]; break
                if lo[j] <= st: x = st; break
                if df["high"].iloc[j] >= tp: x = max(tp, o[j]); break
                if c.iloc[j] > s5.iloc[j] or j - i >= hold: x = c.iloc[j]; break
                j += 1
            if x is None:
                break
            ef, xf = e * (1 + slip), x * (1 - slip)
            out.append({"sym": sym, "in": df.index[i], "out": df.index[j], "r": (xf - ef) / (ef - st)})
            i = j + 1
        else:
            i += 1
    return out


def tune_swing(s, data: dict[str, pd.DataFrame], spy: pd.DataFrame, now: datetime) -> dict:
    from app.regime import regime_series
    cells = set((s.regime.matrix.get("stock") or {}).get("rsi2_swing") or [])
    rg = regime_series(spy).dropna(subset=["trend", "vol"])
    lab = (rg["trend"] + "/" + rg["vol"])
    lab.index = lab.index.tz_convert("America/New_York").normalize()

    def reg_ok(ts):                                 # regime of the last COMPLETED day before the entry day
        d = pd.Timestamp(ts).tz_convert("America/New_York").normalize()
        prev = lab[lab.index < d]
        return len(prev) > 0 and prev.iloc[-1] in cells
    slip = s.execution.slippage_bps_stock / 1e4
    cut = pd.Timestamp(now - timedelta(days=365))

    def evaluate(rsi_max, k):
        ts = [t for sym, df in data.items() for t in rsi2_trades(df, sym, rsi_max, k, s.swing.max_hold_days, slip, reg_ok)]
        return {"is": stats([t["r"] for t in portfolio([t for t in ts if t["in"] < cut])]),
                "oos": stats([t["r"] for t in portfolio([t for t in ts if t["in"] >= cut])])}
    base = {"swing.rsi_max": float(s.swing.rsi_max), "swing.atr_mult": float(s.swing.atr_mult)}
    cur = evaluate(base["swing.rsi_max"], base["swing.atr_mult"])
    cands = {}
    for key in base:
        for v in LP.neighbours(key, base[key]):
            p = dict(base, **{key: v})
            cands[(key, v)] = evaluate(p["swing.rsi_max"], p["swing.atr_mult"])
    return {"current": cur, "candidates": {f"{k}={v:g}": r for (k, v), r in cands.items()}, "_cands": cands,
            "base": base}


def load_swing_data(s, secrets) -> tuple[dict, pd.DataFrame | None]:
    from app.backtest import history as H
    out = {}
    for sym in s.markets.stocks.watchlist:
        try:
            df = H.load("alpaca", sym, "1d", 2000, secrets, max_age_hours=96)
            if len(df) > 260:
                out[sym] = df[["open", "high", "low", "close", "volume"]]
        except Exception as e:
            print(f"swing data {sym}: {type(e).__name__}: {e}"[:160], file=sys.stderr)
    return out, out.get(s.regime.stock_benchmark)


# ------------------------------------------------------------------ crypto (D99 strategies, 15m, 60 d)
def tune_crypto(s, days: int = 60, oos_days: int = 20) -> dict:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "research"))
    from concurrent.futures import ProcessPoolExecutor

    import tune
    from app.backtest import history as H
    from app.regime import regime_series
    for sym in s.markets.crypto.watchlist:
        try:
            H.load("coinbase", sym, "15m", days, max_age_hours=96)
        except Exception as e:
            print(f"crypto data {sym}: {type(e).__name__}: {e}"[:160], file=sys.stderr)
    btc = H.load("coinbase", s.regime.crypto_benchmark, "1d", 400, max_age_hours=96)
    rg = regime_series(btc).dropna(subset=["trend", "vol"])
    lab = rg["trend"] + "/" + rg["vol"]
    matrix = s.regime.matrix.get("crypto") or {}
    with ProcessPoolExecutor(4) as ex:
        cands = [c for r in ex.map(tune.gen, [(x, "crypto", days) for x in s.markets.crypto.watchlist]) for c in r]
    if not cands:
        return {"error": "no crypto history"}
    end = max(pd.Timestamp(c["ts"]) for c in cands)
    split = end - pd.Timedelta(days=oos_days)
    enabled = {n for n, t in s.scanner.crypto_strategies.items() if t.enabled}

    def ok(c):
        d = pd.Timestamp(c["ts"]).normalize()
        prev = lab[lab.index < d]
        cells = matrix.get(c["strategy"])
        return c["strategy"] in enabled and (cells is None or (len(prev) and prev.iloc[-1] in cells))
    cands = [c for c in cands if ok(c)]

    def evaluate(k_sl, k_tp, rvol):
        p = tune.Policy("x", mode="atr", k_sl=k_sl, k_tp=k_tp, cap="unless_momentum", fees="maker", gate="taker",
                        min_rvol=rvol, min_rr=max(LP.MIN_RR, 1.2))
        ts = [t for c in cands if tune.passes(c, p) for t in [tune.trade(c, p, days)] if t]
        seg = lambda sel: stats([t["r"] for t in tune.portfolio(sel)])
        return {"is": seg([t for t in ts if t["entry_ts"] < split]), "oos": seg([t for t in ts if t["entry_ts"] >= split])}
    base = {"risk.atr_stop_mult": float(s.risk.atr_stop_mult), "risk.atr_target_mult": float(s.risk.atr_target_mult),
            "scanner.crypto_min_rvol": float(s.scanner.crypto_min_rvol)}
    cur = evaluate(*base.values())
    res = {}
    for key in base:
        for v in LP.neighbours(key, base[key]):
            p = dict(base, **{key: v})
            if p["risk.atr_target_mult"] / p["risk.atr_stop_mult"] < LP.MIN_RR:      # hard rail
                continue
            res[(key, v)] = evaluate(*p.values())
    return {"current": cur, "candidates": {f"{k}={v:g}": r for (k, v), r in res.items()}, "_cands": res, "base": base}


# ------------------------------------------------------------------ live history + auto-revert
CRYPTO_GROUP = "crypto"


def live_r(db, group: str, after: str | None = None, before: str | None = None, last: int | None = None) -> list[float]:
    cond = "asset_class='stock' AND strategy='rsi2_swing'" if group == "rsi2_swing" else "asset_class='crypto'"
    sql = f"SELECT r_multiple FROM positions WHERE status='closed' AND r_multiple IS NOT NULL AND {cond}"
    args: list = []
    if after:
        sql += " AND closed_ts >= ?"; args.append(after)
    if before:
        sql += " AND closed_ts < ?"; args.append(before)
    sql += " ORDER BY closed_ts DESC" + (f" LIMIT {int(last)}" if last else "")
    return [r[0] for r in db.conn.execute(sql, args).fetchall()]


def check_reverts(doc: dict, db, cfg, now: datetime) -> list[dict]:
    """Revert the latest live change of a group when its live trades since then degraded."""
    out = []
    for group in ("rsi2_swing", CRYPTO_GROUP):
        hist = [h for h in doc["history"] if h["group"] == group]
        if not hist or hist[-1]["kind"] != "param_change":
            continue
        h = hist[-1]
        after = live_r(db, group, after=h["ts"])
        if len(after) < cfg.revert_min_trades:
            continue
        before = live_r(db, group, before=h["ts"], last=len(after))
        a, b = float(np.mean(after)), (float(np.mean(before)) if before else 0.0)
        if a < b - cfg.revert_drop_r or a < cfg.revert_floor_r:
            why = (f"live results got worse after the change ({a:+.2f} R per trade over {len(after)} trades "
                   f"vs {b:+.2f} R before)")
            LP.record(doc, "param_revert", h["key"], h["new"], h["old"], why, {"live_after": a, "live_before": b}, now)
            out.append(doc["history"][-1])
    return out


def weekly(s, db, secrets, now: datetime | None = None, groups=("rsi2_swing", "crypto"), swing_data=None,
           crypto_result=None) -> dict:
    """Run the weekly job; returns the report and updates var/params.json (versioned)."""
    now = now or datetime.now(timezone.utc)
    cfg = s.learning
    doc = LP.load()
    report = {"ts": now.isoformat(), "reverts": [], "groups": {}}
    report["reverts"] = check_reverts(doc, db, cfg, now)
    reverted = {h["group"] for h in report["reverts"]}
    for g in groups:
        live = live_r(db, g, last=cfg.window)
        entry = {"live": stats(live)}
        if g in reverted:
            entry["decision"] = "reverted a previous change; no new change this week"
            report["groups"][g] = entry
            continue
        try:
            if g == "rsi2_swing":
                data, spy = swing_data if swing_data is not None else load_swing_data(s, secrets)
                r = tune_swing(s, data, spy, now) if data and spy is not None else {"error": "no daily stock data"}
                min_n = cfg.min_oos_trades_swing
            else:
                r = crypto_result if crypto_result is not None else tune_crypto(s)
                min_n = cfg.min_oos_trades_crypto
        except Exception as e:
            r = {"error": f"{type(e).__name__}: {e}"}
        if "error" in r:
            entry.update(error=r["error"], decision="no change (backtest unavailable)")
            report["groups"][g] = entry
            continue
        key, val, why = decide(r["current"], r["_cands"], min_n, cfg.min_gain_r)
        entry.update(current=r["current"], candidates=r["candidates"], decision=why if key is None else "changed")
        if key is not None:
            old = r["base"][key]
            LP.record(doc, "param_change", key, old, val, why,
                      {"oos": r["_cands"][(key, val)]["oos"], "oos_current": r["current"]["oos"]}, now)
            entry["change"] = doc["history"][-1]
        report["groups"][g] = entry
    doc["last_run"] = {"ts": now.isoformat(), "summary": {g: e.get("decision") for g, e in report["groups"].items()},
                       "reverts": len(report["reverts"])}
    LP.save(doc)
    return report
