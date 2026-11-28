"""D105 sanity check (paper research only): does the analyst consensus cut good trades out-of-sample?

Replays the same candidates the live bot would see (crypto D99 strategies 15m / 60 d with the current ATR policy,
regime-gated; overnight dip-buy on daily bars / 365 d), with and without the Market + Technical analysts (+ neutral
News / Strategy, which cannot be reconstructed historically). Reports trades and average R before / after.
Usage: python scripts/research/agents_oos.py"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

import tune  # noqa: E402
from app.agents import analysts as A  # noqa: E402
from app.agents.consensus import combine  # noqa: E402
from app.backtest import history as H  # noqa: E402
from app.learning import tuner as T  # noqa: E402
from app.regime import regime_series  # noqa: E402

S = tune.S


def seg(ts, key="r"):
    rs = [t[key] for t in ts]
    return {"n": len(rs), "avg_r": round(float(np.mean(rs)), 3) if rs else 0.0,
            "total_r": round(float(np.sum(rs)), 1) if rs else 0.0,
            "win": round(float(np.mean([r > 0 for r in rs])), 3) if rs else 0.0}


def crypto(variants):
    cf = Path("/tmp/tune_cands.json")
    cands = [c for c in json.loads(cf.read_text()) if c["ac"] == "crypto"]
    btc = H.load("coinbase", S.regime.crypto_benchmark, "1d", 400, max_age_hours=10 ** 6)
    rg = regime_series(btc).dropna(subset=["trend", "vol"])
    lab = rg["trend"] + "/" + rg["vol"]
    end = max(pd.Timestamp(c["ts"]) for c in cands)
    split = end - pd.Timedelta(days=20)
    enabled = {n for n, t in S.scanner.crypto_strategies.items() if t.enabled}
    matrix = S.regime.matrix.get("crypto") or {}
    pol = tune.Policy("x", mode="atr", k_sl=S.risk.atr_stop_mult, k_tp=S.risk.atr_target_mult, cap="unless_momentum",
                      fees="maker", gate="taker", min_rvol=S.scanner.crypto_min_rvol, min_rr=1.2)
    rows = []
    for c in cands:
        prev = lab[lab.index < pd.Timestamp(c["ts"]).normalize()]
        cells = matrix.get(c["strategy"])
        if c["strategy"] not in enabled or not (cells is None or (len(prev) and prev.iloc[-1] in cells)):
            continue
        if not tune.passes(c, pol):
            continue
        t = tune.trade(c, pol, 60)
        if not t:
            continue
        reg = {"trend": prev.iloc[-1].split("/")[0], "vol": prev.iloc[-1].split("/")[1]} if len(prev) else None
        trend = "up" if c["h1"] == 1.0 else ("down" if c["entry"] < c["ema50"] else "range")
        feat = {"asset_class": "crypto", "strategy": c["strategy"], "rr_net": t["rr"], "rsi": c["rsi"], "rvol": c["rvol"],
                "res_dist_atr": ((c["res"] - c["entry"]) / c["atr"]) if c["res"] and c["atr"] else None,
                "trend": trend, "regime": reg, "regime_ok": True, "validation_status": "validated"}
        rows.append((t, feat))
    return rows, split


def swing():
    data, spy = T.load_swing_data(S, None)
    rg = regime_series(spy).dropna(subset=["trend", "vol"])
    lab = rg["trend"] + "/" + rg["vol"]
    lab.index = lab.index.tz_convert("America/New_York").normalize()
    cells = set((S.regime.matrix.get("stock") or {}).get("rsi2_swing") or [])
    slip = S.execution.slippage_bps_stock / 1e4
    cut = pd.Timestamp(datetime.now(timezone.utc) - pd.Timedelta(days=365))
    rows = []
    for sym, df in data.items():
        rv = (df["volume"] / df["volume"].rolling(20).mean()).shift(0)
        def reg_ok(ts):
            d = pd.Timestamp(ts).tz_convert("America/New_York").normalize()
            prev = lab[lab.index < d]
            return len(prev) > 0 and prev.iloc[-1] in cells
        for t in T.rsi2_trades(df, sym, S.swing.rsi_max, S.swing.atr_mult, S.swing.max_hold_days, slip, reg_ok):
            d = pd.Timestamp(t["in"]).tz_convert("America/New_York").normalize()
            prev = lab[lab.index < d]
            r0 = prev.iloc[-1].split("/")
            feat = {"asset_class": "stock", "strategy": "rsi2_swing", "rr_net": 2.0, "rsi": None,
                    "rvol": float(rv.loc[t["in"]]) if t["in"] in rv.index and rv.loc[t["in"]] == rv.loc[t["in"]] else None,
                    "res_dist_atr": None, "trend": None, "regime": {"trend": r0[0], "vol": r0[1]}, "regime_ok": True,
                    "validation_status": "validated"}
            t = dict(t, entry_ts=t["in"], exit_ts=t["out"], rr=2.0, sym=sym)
            rows.append((t, feat))
    return rows, cut


def verdict(feat):
    v = {"market": A.market_analyst(feat), "technical": A.technical_analyst(feat),
         "news": A.news_analyst(feat), "strategy": A.strategy_analyst(feat)}
    return v


def evaluate(rows, split, portfolio):
    res = {}
    modes = {"before (no analysts)": None, "advisory (clear rejects only)": False, "enforced (score gates)": True}
    for name, enf in modes.items():
        kept = []
        for t, f in rows:
            if enf is None:
                kept.append(dict(t, w=1.0)); continue
            c = combine(verdict(f), enforce=enf)
            if c.blocked_by is None:
                kept.append(dict(t, w=c.mult))
        kept = portfolio(kept)
        for x in kept:
            x["rw"] = x["r"] * x["w"]
        res[name] = {"in_sample": seg([t for t in kept if t["entry_ts"] < split]),
                     "out_of_sample": seg([t for t in kept if t["entry_ts"] >= split]),
                     "oos_size_weighted_total_r": round(sum(t["rw"] for t in kept if t["entry_ts"] >= split), 1)}
    return res


def main():
    out = {}
    rows, split = crypto(None)
    out["crypto"] = evaluate(rows, split, tune.portfolio)
    try:
        srows, cut = swing()
        port = lambda ts: T.portfolio([dict(t, **{"in": t["entry_ts"], "out": t["exit_ts"]}) for t in ts])
        out["overnight_dip_buy"] = evaluate(srows, cut, port)
    except Exception as e:
        out["overnight_dip_buy"] = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
