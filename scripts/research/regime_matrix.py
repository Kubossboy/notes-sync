"""D100 regime x strategy matrix for crypto (D99 exits) - BTC daily regime at entry.
IS = 3 majors (730 d of 15m) before 2026-06-01; OOS = majors after + all 40 pairs (60 d)."""
import json, sys
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
sys.path.insert(0, "scripts/research"); sys.path.insert(0, ".")
import tune
from tune import Policy
from app.regime import regime_series

CUT = pd.Timestamp("2026-06-01", tz="UTC")
POL = Policy("D101", mode="atr", k_sl=1.5, k_tp=2.0, cap="unless_momentum", fees="maker", gate="taker")

def sim(args):
    cands, days = args
    return [t for c in cands for t in [tune.trade(c, POL, days)] if t]

if __name__ == "__main__":
    btc = pd.read_csv("var/history/btc_1d.csv.gz", index_col=0, parse_dates=True); btc.index = pd.to_datetime(btc.index, utc=True)
    reg = regime_series(btc).dropna(subset=["trend", "vol"]); lab = reg["trend"] + "/" + reg["vol"]
    with ProcessPoolExecutor(3) as ex:
        big = [c for r in ex.map(tune.gen, [(s, "crypto", 730) for s in ("BTC-USD", "ETH-USD", "SOL-USD")]) for c in r]
    small, _ = tune.main(60, 20)
    jobs = [([c for c in big if c["sym"] == s], 730) for s in ("BTC-USD", "ETH-USD", "SOL-USD")]
    syms = sorted({c["sym"] for c in small})
    jobs += [([c for c in small if c["sym"] == s], 60) for s in syms]
    with ProcessPoolExecutor(7) as ex:
        trades = [t for r in ex.map(sim, jobs) for t in r]
    for t in trades:
        d = pd.Timestamp(t["entry_ts"]).normalize()
        prev = lab[lab.index < d]
        t["reg"] = prev.iloc[-1] if len(prev) else None
        t["seg"] = "is" if (t["entry_ts"] < CUT) else "oos"
    out = {}
    for st in sorted({t["strategy"] for t in trades}):
        for c in sorted({t["reg"] for t in trades if t["reg"]}):
            for seg in ("is", "oos"):
                sel = [t for t in trades if t["strategy"] == st and t["reg"] == c and t["seg"] == seg]
                out.setdefault(st, {}).setdefault(c, {})[seg] = tune.stats(tune.portfolio(sel))
    json.dump(out, open("/tmp/crypto_matrix.json", "w"), default=str, indent=1)
    allr = {}
    for seg in ("is", "oos"):
        allr[seg] = tune.stats(tune.portfolio([t for t in trades if t["seg"] == seg]))
    json.dump({"trades": [{k: (str(v) if k.endswith("ts") else v) for k, v in t.items()} for t in trades]}, open("/tmp/crypto_trades.json", "w"))
    print("ALL", allr)
    for st, cs in out.items():
        print(st)
        for c, v in cs.items():
            i, o = v["is"], v["oos"]
            print(f"   {c:14s} IS n{i.get('trades',0):4d} wr{i.get('win_rate',0):.2f} R{i.get('expectancy_r',0):+.3f} | OOS n{o.get('trades',0):4d} wr{o.get('win_rate',0):.2f} R{o.get('expectancy_r',0):+.3f}")
