"""Render docs/BACKTEST_RESULTS.md from `backtest --json` outputs (var/backtest_*.json)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def f(x, nd=2):
    return "–" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def table(out):
    rows = [f"### {out['asset_class']} · {out['source']} {out['timeframe']} · {', '.join(out['symbols'])} · "
            f"{out['period'][0][:10]} → {out['period'][1][:10]}", "",
            "| strategy | IS n | IS PF | OOS n | OOS win % | OOS PF | OOS exp R | OOS net % | OOS max DD % | "
            "WF n | WF PF | WF + folds | stress PF | loose / strict OOS PF | diag. R:R≥1 OOS n / PF / exp R | verdict |",
            "|---|" + "---|" * 15]
    for n, r in out["strategies"].items():
        if "out_of_sample" not in r:
            rows.append(f"| {n} | | | | | | | | | | | | | | | not backtestable: {r['reasons'][0]} |")
            continue
        i, o, w, s = r["in_sample"], r["out_of_sample"], r["walk_forward"], r["stress_oos"]
        se = r["sensitivity_oos"]
        dx = (r.get("diagnostic_rr_gate_1") or {}).get("out_of_sample", {})
        rows.append(
            f"| {n} | {i['trades']} | {f(i.get('profit_factor'))} | {o['trades']} | "
            f"{f(o['win_rate'] * 100, 0) if o['trades'] else '–'} | {f(o.get('profit_factor'))} | "
            f"{f(o.get('expectancy_r'), 3)} | {f(o.get('net_return_pct'))} | {f(o.get('max_drawdown_pct'))} | "
            f"{w['combined_oos']['trades']} | {f(w['combined_oos'].get('profit_factor'))} | "
            f"{w['positive_folds']}/{w['folds_with_trades']} | {f(s.get('profit_factor'))} | "
            f"{f(se['loose'].get('profit_factor'))} / {f(se['strict'].get('profit_factor'))} | "
            f"{dx.get('trades', 0)} / {f(dx.get('profit_factor'))} / {f(dx.get('expectancy_r'), 3)} | **{r['verdict']}** |")
    return "\n".join(rows)


def main(files):
    parts = []
    for fn in files:
        p = ROOT / fn
        if p.exists():
            for out in json.loads(p.read_text()):
                parts.append(table(out))
    print("\n\n".join(parts))


if __name__ == "__main__":
    main(sys.argv[1:] or ["var/backtest_crypto.json", "var/backtest_stock.json", "var/backtest_stock_yahoo.json"])
