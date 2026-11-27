"""Filter / exit-policy research on free history (paper research only; nothing here trades).

1) Generates every RAW setup of the backtestable live strategies (same strategy code as the scanner) on
   Coinbase 15m crypto (watchlist) and Alpaca 5m stocks (cached), with causal features incl. 1h / 4h trend.
2) Applies exit/filter POLICIES (baseline = the live pipeline: resistance-capped plan + net R:R gate +
   100x scalp levels; candidates = ATR stops/targets, HTF trend, volume, RSI regime, strategy subsets).
3) Simulates fills with the live CostModel + taker fees, stop-first, crypto 24 h max hold, stock EOD flatten.
4) Portfolio replay (max 2 open, one per symbol, best expected value first) on an in-sample period and a
   later out-of-sample holdout. Reports trades, win rate, expectancy (R, after fees), max drawdown (R).
Usage: python scripts/research/tune.py [days=60] [oos_days=20]
"""
from __future__ import annotations

import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.backtest.history import CACHE  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.data.calendar import ET  # noqa: E402
from app.data.models import AssetClass  # noqa: E402
from app.execution.costs import CostModel  # noqa: E402
from app.scanners.scoring import cost_bps_per_side  # noqa: E402
from app.strategies.base import NoSetup, StrategyContext  # noqa: E402
from app.strategies.crypto import CRYPTO_STRATEGIES  # noqa: E402
from app.strategies.stock import STOCK_STRATEGIES  # noqa: E402
from app.technical.indicators import compute_all, support_resistance  # noqa: E402

S = load_settings()
CM = CostModel(S)
CRYPTO_BT = ["momentum_breakout", "volume_expansion", "volatility_expansion", "trend_continuation", "sr_breakout"]
STOCK_BT = ["opening_range_breakout", "vwap_reclaim", "momentum_breakout", "relative_volume_anomaly",
            "trend_continuation", "sr_breakout"]
STOCKS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META"]


def _htf(ind: pd.DataFrame, rule: str, bar: pd.Timedelta) -> pd.Series:
    """True when the last COMPLETED higher-timeframe bar has EMA20 > EMA50 and close > EMA20 (no look-ahead)."""
    c = ind["close"].resample(rule, label="left", closed="left").last().dropna()
    e20, e50 = c.ewm(span=20, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
    up = ((e20 > e50) & (c > e20)).astype(float)
    up.index = up.index + pd.Timedelta(rule)          # available once the HTF bar has closed
    avail = ind.index + bar                           # a 15m bar is usable at its close
    return pd.Series(up.reindex(up.index.union(avail)).ffill().reindex(avail).to_numpy(), index=ind.index)


def load_raw(sym: str, ac: str, days: int) -> pd.DataFrame | None:
    if ac == "crypto":
        f = CACHE / f"coinbase_{sym}_15m_60d.csv.gz"
    else:
        f = CACHE / f"alpaca_{sym}_5m_365d.csv.gz"
    if not f.exists():
        return None
    df = pd.read_csv(f, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)
    return df[df.index >= df.index[-1] - pd.Timedelta(days=days)]


def gen(args) -> list[dict]:
    sym, ac, days = args
    raw = load_raw(sym, ac, days)
    if raw is None or len(raw) < 400:
        return []
    acE = AssetClass(ac)
    base = raw[raw["session"] == "regular"] if ac == "stock" and "session" in raw else raw
    ind = compute_all(base, acE)
    tf = "15m" if ac == "crypto" else "5m"
    bar = pd.Timedelta(minutes=15 if ac == "crypto" else 5)
    h1, h4 = _htf(ind, "1h", bar), _htf(ind, "4h", bar)
    hi20 = ind["high"].rolling(20).max().shift(1)
    table = CRYPTO_STRATEGIES if ac == "crypto" else STOCK_STRATEGIES
    names = CRYPTO_BT if ac == "crypto" else STOCK_BT
    toggles = S.scanner.crypto_strategies if ac == "crypto" else S.scanner.stock_strategies
    cost = cost_bps_per_side(S, acE)
    out, levels = [], []
    warm = 250
    for i in range(warm, len(ind) - 1):
        d = ind.iloc[max(0, i - 399): i + 1]
        if (i - warm) % 4 == 0:
            levels = support_resistance(d)
        ctx = StrategyContext(sym, acE, tf, d, None, levels, None, [], d.index[-1].to_pydatetime(), cost)
        for n in names:
            t = toggles.get(n)
            try:
                st = table[n](ctx, dict(t.params) if t else {})
            except NoSetup:
                continue
            except Exception:
                continue
            last = d.iloc[-1]
            e = st.entry
            risk = e - st.stop
            res = sorted(L["price"] for L in levels if L["touches"] >= 2 and "high" in L["kinds"] and L["price"] > e + 0.25 * max(risk, 0))
            out.append({"sym": sym, "ac": ac, "strategy": n, "i": i, "ts": d.index[-1].isoformat(), "entry": e, "stop": st.stop,
                        "target_r": st.target_r, "atr": float(last["atr14"]), "rsi": float(last.get("rsi14", np.nan)),
                        "rvol": float(last.get("rvol20", np.nan)), "ema50": float(last.get("ema50", np.nan)),
                        "res": res[0] if res else None, "h1": h1.iloc[i], "h4": h4.iloc[i],
                        "brk": bool(last["close"] > hi20.iloc[i]) if hi20.iloc[i] == hi20.iloc[i] else False})
    return out


_BARS: dict = {}


def bars(sym, ac, days):
    k = (sym, ac)
    if k not in _BARS:
        raw = load_raw(sym, ac, days)
        base = raw[raw["session"] == "regular"] if ac == "stock" and "session" in raw else raw
        _BARS[k] = base
    return _BARS[k]


@dataclass
class Policy:
    name: str
    mode: str = "live"          # live | atr
    k_sl: float = 1.5
    k_tp: float = 2.5
    cap: str = "always"         # always | unless_momentum | never
    min_rr: float = 1.2
    htf: str = ""               # "" | h1 | h4 | h1h4
    min_rvol: float = 0.0
    rsi: tuple | None = None
    strategies: frozenset | None = None
    min_rvol_stock: float = 0.0


def levels_for(c: dict, p: Policy, fill: float) -> tuple[float, float] | None:
    """(stop, target) at the actual fill, or None if the plan is rejected."""
    ac = AssetClass(c["ac"])
    atr, e = c["atr"], c["entry"]
    if p.mode in ("live", "plan"):
        risk = e - c["stop"]
        if risk <= 0 or risk < 0.25 * atr or risk > S.scanner.max_stop_atr * atr:
            return None
        tgt = e + c["target_r"] * risk
        mom = c["brk"] and (c["rvol"] or 0) >= 1.5
        if c["res"] is not None and e + 0.25 * risk < c["res"] < tgt:
            if p.cap == "always" or (p.cap == "unless_momentum" and not mom):
                tgt = c["res"]
        cst = e * 2 * cost_bps_per_side(S, ac) / 1e4
        if (tgt - e - cst) / (risk + cst) < p.min_rr:
            return None
        if p.mode == "plan":
            return c["stop"], tgt
        # D94 100x scalp levels at the fill
        if not (c["stop"] < fill < tgt):
            return None
        tp_d = min(tgt - fill, S.risk.lev_tp_atr * atr)
        sl_d = min(fill - c["stop"], tp_d / 1.5, S.risk.lev_sl_liq_fraction * fill * (1 / 100 - S.execution.maintenance_margin_pct / 100))
        return fill - sl_d, fill + tp_d
    sl_d, tp_d = p.k_sl * atr, p.k_tp * atr
    tgt = fill + tp_d
    if c["res"] is not None and fill < c["res"] < tgt:
        mom = c["brk"] and (c["rvol"] or 0) >= 1.5
        if p.cap == "always" or (p.cap == "unless_momentum" and not mom):
            tgt = c["res"]
    return fill - sl_d, tgt


def net_rr(fill, stop, tgt, ac) -> float:
    fee = (S.execution.crypto_fee_bps if ac == "crypto" else S.execution.stock_fee_bps) / 1e4
    slip = CM.slip(AssetClass(ac))
    win = tgt - fill - fee * (fill + tgt)
    loss = fill - stop * (1 - slip) + fee * (fill + stop)
    return win / loss if loss > 0 else -9


def passes(c: dict, p: Policy) -> bool:
    if p.strategies is not None and f"{c['ac']}:{c['strategy']}" not in p.strategies:
        return False
    if p.htf in ("h1", "h1h4") and c["h1"] != 1.0:
        return False
    if p.htf in ("h4", "h1h4") and c["h4"] != 1.0:
        return False
    if p.min_rvol and not (c["rvol"] >= p.min_rvol):
        return False
    if c["ac"] == "stock" and p.min_rvol_stock and not (c["rvol"] >= p.min_rvol_stock):
        return False
    if p.rsi and not (p.rsi[0] <= c["rsi"] <= p.rsi[1]):
        return False
    return True


def trade(c: dict, p: Policy, days: int) -> dict | None:
    ac = c["ac"]
    acE = AssetClass(ac)
    df = bars(c["sym"], ac, days)
    j = c["i"] + 1
    if j >= len(df):
        return None
    o, h, l, cl = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = df.index
    fill = CM.buy_fill(acE, float(o[j])).price
    lv = levels_for(c, p, fill)
    if lv is None:
        return None
    stop, tgt = lv
    if not (stop < fill < tgt) or net_rr(fill, stop, tgt, ac) < p.min_rr:
        return None
    fee = (S.execution.crypto_fee_bps if ac == "crypto" else S.execution.stock_fee_bps) / 1e4
    unit = fill - stop * (1 - CM.slip(acE)) + fee * (fill + stop)
    t0 = idx[j]
    day = t0.tz_convert(ET).date()
    k, ex, why = j, None, None
    while k < len(df):
        if ac == "stock" and idx[k].tz_convert(ET).date() != day:
            ex, why, k = CM.sell_fill(acE, float(cl[k - 1])).price, "eod", k - 1
            break
        if l[k] <= stop:
            ex, why = CM.stop_fill(acE, stop, float(o[k])).price, "sl"
            break
        if h[k] > tgt:
            ex, why = CM.target_fill(acE, tgt, float(o[k])).price, "tp"
            break
        if ac == "crypto" and idx[k] - t0 >= pd.Timedelta(hours=S.risk.crypto_max_hold_hours):
            ex, why = CM.sell_fill(acE, float(cl[k])).price, "time"
            break
        k += 1
    if ex is None:
        return None
    r = (ex - fill - fee * (fill + ex)) / unit
    return {"sym": c["sym"], "ac": ac, "strategy": c["strategy"], "entry_ts": t0, "exit_ts": idx[min(k, len(df) - 1)],
            "r": r, "why": why, "rr": net_rr(fill, stop, tgt, ac), "sl_pct": (fill - stop) / fill * 100}


def portfolio(trades: list[dict], max_open: int = 2) -> list[dict]:
    """Chronological, max_open at once, one per symbol; same-bar candidates ranked by expected value (net R:R)."""
    trades = sorted(trades, key=lambda t: (t["entry_ts"], -t["rr"]))
    open_, taken = [], []
    for t in trades:
        open_ = [o for o in open_ if o["exit_ts"] > t["entry_ts"]]
        if len(open_) >= max_open or any(o["sym"] == t["sym"] for o in open_):
            continue
        open_.append(t)
        taken.append(t)
    return taken


def stats(ts: list[dict]) -> dict:
    if not ts:
        return {"trades": 0}
    r = np.array([t["r"] for t in ts])
    eq = np.cumsum(r)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], eq]))[1:] - eq)) if len(eq) else 0.0
    wins, losses = r[r > 0], r[r <= 0]
    return {"trades": len(r), "win_rate": round(float((r > 0).mean()), 3), "expectancy_r": round(float(r.mean()), 3),
            "total_r": round(float(r.sum()), 1), "max_dd_r": round(dd, 1),
            "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else None}


def run(cands: list[dict], policies: list[Policy], split_ts: pd.Timestamp, days: int) -> dict:
    out = {}
    for p in policies:
        ts = []
        for c in cands:
            if passes(c, p):
                t = trade(c, p, days)
                if t:
                    ts.append(t)
        res = {}
        for seg, sel in (("in_sample", [t for t in ts if t["entry_ts"] < split_ts]),
                         ("out_of_sample", [t for t in ts if t["entry_ts"] >= split_ts])):
            res[seg] = {"all": stats(portfolio(sel))}
            for ac in ("crypto", "stock"):
                res[seg][ac] = stats(portfolio([t for t in sel if t["ac"] == ac]))
        by = {}
        for t in [t for t in ts if t["entry_ts"] < split_ts]:
            by.setdefault(f"{t['ac']}:{t['strategy']}", []).append(t)
        res["in_sample_by_strategy"] = {k: stats(v) for k, v in sorted(by.items())}
        out[p.name] = res
    return out


def main(days: int = 60, oos_days: int = 20):
    jobs = [(s, "crypto", days) for s in S.markets.crypto.watchlist] + [(s, "stock", days) for s in STOCKS]
    cf = Path("/tmp/tune_cands.json")
    if cf.exists():
        cands = json.loads(cf.read_text())
    else:
        with ProcessPoolExecutor(7) as ex:
            cands = [c for r in ex.map(gen, jobs) for c in r]
        cf.write_text(json.dumps(cands))
    end = max(pd.Timestamp(c["ts"]) for c in cands)
    split = end - pd.Timedelta(days=oos_days)
    return cands, split


if __name__ == "__main__":
    cands, split = main(*(int(x) for x in sys.argv[1:3]))
    print(len(cands), "raw setups; holdout starts", split)
