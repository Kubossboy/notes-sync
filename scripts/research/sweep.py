"""Policy sweep over tune.py setups: choose on in-sample, report holdout."""
import json, sys
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, "scripts/research")
import tune
from tune import Policy

C_GOOD = frozenset({"crypto:momentum_breakout", "crypto:volume_expansion", "crypto:volatility_expansion"})
ALL = None

def policies():
    ps = [Policy("baseline_live")]
    for k_sl in (1.0, 1.5, 2.0):
        for k_tp in (1.5, 2.0, 3.0):
            for cap in ("always", "unless_momentum"):
                ps.append(Policy(f"atr_sl{k_sl}_tp{k_tp}_{cap}", mode="atr", k_sl=k_sl, k_tp=k_tp, cap=cap))
    return ps

def report(out):
    for n, r in out.items():
        i, o = r["in_sample"], r["out_of_sample"]
        f = lambda s: f"{s.get('trades',0):4d} wr{s.get('win_rate',0):.2f} E{s.get('expectancy_r',0):+.3f} dd{s.get('max_dd_r',0):5.1f}"
        print(f"{n:38s} IS[c {f(i['crypto'])} | s {f(i['stock'])}]  OOS[c {f(o['crypto'])} | s {f(o['stock'])}]")

def one(p):
    cands, split = tune.main(60, 20)
    return tune.run(cands, [p], split, 60)

if __name__ == "__main__":
    ps = policies() if len(sys.argv) < 2 else eval(sys.argv[1])
    out = {}
    with ProcessPoolExecutor(7) as ex:
        for r in ex.map(one, ps):
            out.update(r)
    json.dump(out, open(sys.argv[2] if len(sys.argv) > 2 else "/tmp/sweep1.json", "w"), default=str, indent=1)
    for n, r in out.items():
        i, o = r["in_sample"], r["out_of_sample"]
        f = lambda s: f"{s.get('trades',0):4d} wr{s.get('win_rate',0):.2f} E{s.get('expectancy_r',0):+.3f} dd{s.get('max_dd_r',0):5.1f}"
        print(f"{n:38s} IS[c {f(i['crypto'])} | s {f(i['stock'])}]  OOS[c {f(o['crypto'])} | s {f(o['stock'])}]")
