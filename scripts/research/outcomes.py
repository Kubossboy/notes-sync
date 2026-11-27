"""What happened AFTER each journaled candidate (paper research, read-only).

For every signal in the state DB (rejected, new, incomplete, executed) walk the next N bars after its data bar:
TP first, SL first (stop wins ties, like the simulator) or neither (time exit at the last close). Net R after
the live cost model (fees + slippage both sides). Groups by status, strategy and reject reason.
Usage: python scripts/research/outcomes.py <state.sqlite3>
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.backtest.history import CACHE, alpaca_history, coinbase_history  # noqa: E402
from app.config import load_secrets, load_settings  # noqa: E402
from app.data.models import AssetClass  # noqa: E402
from app.scanners.scoring import cost_bps_per_side  # noqa: E402


def reason_kind(r: str | None) -> str:
    if not r:
        return "-"
    out = []
    for p in r.split("; "):
        if p.startswith("net reward/risk"):
            out.append("low net R:R (capped by resistance)" if "capped" in p else "low net R:R")
        else:
            out.append(p.split(" ")[0] + " " + " ".join(p.split(" ")[1:3]))
    return " + ".join(sorted(set(out)))


def main(db_path: str, horizon_crypto: int = 96, horizon_stock: int = 78) -> dict:
    s, sec = load_settings(), load_secrets()
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("SELECT * FROM signals ORDER BY id")]
    bars: dict = {}
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = datetime.fromisoformat(min(r["data_ts"] for r in rows)) - timedelta(hours=1)
    out = []
    for r in rows:
        sym, ac = r["symbol"], r["asset_class"]
        if sym not in bars:
            try:
                f = CACHE / f"coinbase_{sym}_15m_60d.csv.gz"
                if ac == "crypto" and f.exists():
                    df = pd.read_csv(f, index_col=0, parse_dates=True)
                    df.index = pd.to_datetime(df.index, utc=True)
                    if df.index[-1] < pd.Timestamp(end) - pd.Timedelta(hours=2):
                        df = pd.concat([df, coinbase_history(sym, "15m", df.index[-1].to_pydatetime(), end)])
                elif ac == "crypto":
                    df = coinbase_history(sym, "15m", start, end)
                else:
                    df = alpaca_history(sym, "5m", start, end, sec)
                bars[sym] = df[~df.index.duplicated(keep="last")].sort_index()
            except Exception as e:                 # noqa: BLE001
                print("no bars", sym, e, file=sys.stderr)
                bars[sym] = None
        df = bars[sym]
        if df is None or not r["entry_price"] or not r["stop_loss"] or not r["take_profit"]:
            continue
        e, st, tp = r["entry_price"], r["stop_loss"], r["take_profit"]
        if not (st < e < tp):
            continue
        cost = e * 2 * cost_bps_per_side(s, AssetClass(ac)) / 1e4
        risk = e - st + cost
        t0 = pd.Timestamp(r["data_ts"])
        tf = pd.Timedelta(minutes=15 if ac == "crypto" else 5)
        fut = df[df.index > t0][: horizon_crypto if ac == "crypto" else horizon_stock]
        if ac == "stock":
            fut = fut[fut.index.normalize() == (t0 + tf).normalize()]      # same session only (EOD flatten)
        if len(fut) < 2:
            continue
        res, exit_p = "time", float(fut["close"].iloc[-1])
        for _, b in fut.iterrows():
            if b["low"] <= st:
                res, exit_p = "SL", st
                break
            if b["high"] > tp:
                res, exit_p = "TP", tp
                break
        mfe = (float(fut["high"].max()) - e) / (e - st)
        out.append({"id": r["id"], "symbol": sym, "ac": ac, "strategy": r["strategy"], "status": r["status"],
                    "reason": reason_kind(r["reject_reason"]), "rr": r["reward_risk"], "result": res,
                    "net_r": round((exit_p - e - cost) / risk, 3), "mfe_r": round(mfe, 2)})
    return summarize(out)


def summarize(out: list[dict]) -> dict:
    def agg(xs):
        n = len(xs)
        if not n:
            return {}
        return {"n": n, "tp_first": sum(x["result"] == "TP" for x in xs), "sl_first": sum(x["result"] == "SL" for x in xs),
                "win_rate": round(sum(x["net_r"] > 0 for x in xs) / n, 3),
                "avg_net_r": round(sum(x["net_r"] for x in xs) / n, 3),
                "median_mfe_r": sorted(x["mfe_r"] for x in xs)[n // 2]}
    g = defaultdict(list)
    for x in out:
        g[("status", x["status"])].append(x)
        g[("strategy", f"{x['ac']}:{x['strategy']}")].append(x)
        g[("reason", x["reason"])].append(x)
    return {"all": agg(out), "groups": {f"{k[0]}={k[1]}": agg(v) for k, v in sorted(g.items(), key=lambda kv: -len(kv[1]))},
            "rows": out}


if __name__ == "__main__":
    r = main(sys.argv[1])
    print(json.dumps({k: v for k, v in r.items() if k != "rows"}, indent=1))
    Path("/tmp/outcomes_rows.json").write_text(json.dumps(r["rows"]))
