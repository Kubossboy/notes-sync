"""Published stock strategies, adapted for ATLAS (long only), on Alpaca IEX history. Costs: $0 commission,
5 bps slippage per side. Results in R (risk = entry - initial stop) and % of unlevered notional.
Usage: python scripts/research/stock_strats.py"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np, pandas as pd

D = Path(__file__).resolve().parents[2] / "var/history/stk"
SLIP = 5 / 1e4
OOS_DAILY = pd.Timestamp("2025-01-01", tz="UTC")
OOS_INTRA = pd.Timestamp("2026-07-01", tz="UTC")


def daily(sym):
    f = D / f"{sym}_1Day.csv.gz"
    if not f.exists(): return None
    df = pd.read_csv(f, index_col=0, parse_dates=True); df.index = pd.to_datetime(df.index, utc=True)
    return df if len(df) > 260 else None


def intra(sym):
    f = D / f"{sym}_15Min.csv.gz"
    if not f.exists(): return None
    df = pd.read_csv(f, index_col=0, parse_dates=True); df.index = pd.to_datetime(df.index, utc=True)
    et = df.index.tz_convert("America/New_York")
    m = (et.hour * 60 + et.minute >= 570) & (et.hour * 60 + et.minute < 960)
    df = df[m].copy(); df["day"] = df.index.tz_convert("America/New_York").date
    return df if len(df) > 1000 else None


def rsi(c, n):
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df, n):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


SLIP_BY = {}      # D101: 2.5 bps for the 30 most liquid names, 6 bps for the rest ($0 commission)


def rec(sym, strat, t_in, t_out, e, x, stop, why):
    sl = SLIP_BY.get(sym, SLIP)
    ef, xf = e * (1 + sl), x * (1 - sl)
    risk = ef - stop
    return {"sym": sym, "s": strat, "in": t_in, "out": t_out, "r": (xf - ef) / risk if risk > 0 else 0.0,
            "pct": (xf - ef) / ef * 100, "why": why}


# ---------------- daily swing (overnight holds) ----------------
def connors_rsi2(sym, df, p):
    """Connors & Alvarez 2008: close > SMA200 and RSI(2) < 10 -> buy the close; exit close > SMA5.
    Adaptation: protective stop k x ATR(10) below entry (defines R), max hold N days."""
    c = df["close"]; r2 = rsi(c, 2); s200 = c.rolling(200).mean(); s5 = c.rolling(5).mean(); a = atr(df, 10)
    out, i, n = [], 200, len(df)
    while i < n - 1:
        if c.iloc[i] > s200.iloc[i] and r2.iloc[i] < p["rsi"]:
            e = c.iloc[i]; st = e - p["k"] * a.iloc[i]; j = i + 1; x = why = None
            while j < n:
                if df["open"].iloc[j] <= st: x, why = df["open"].iloc[j], "gap_stop"; break
                if df["low"].iloc[j] <= st: x, why = st, "stop"; break
                if c.iloc[j] > s5.iloc[j]: x, why = c.iloc[j], "sma5"; break
                if j - i >= p["hold"]: x, why = c.iloc[j], "time"; break
                j += 1
            if x is None: break
            out.append(rec(sym, "rsi2", df.index[i], df.index[j], e, x, st, why)); i = j + 1
        else:
            i += 1
    return out


def ibs(sym, df, p):
    """Pagonidis 2014 'The IBS Effect': IBS=(C-L)/(H-L) < 0.2 -> buy close, exit next close > prior high.
    Adaptation: only above SMA200, stop k x ATR(10), max hold N days."""
    c = df["close"]; v = (c - df["low"]) / (df["high"] - df["low"]).replace(0, np.nan)
    s200 = c.rolling(200).mean(); a = atr(df, 10); out, i, n = [], 200, len(df)
    while i < n - 1:
        if c.iloc[i] > s200.iloc[i] and v.iloc[i] < p["ibs"]:
            e = c.iloc[i]; st = e - p["k"] * a.iloc[i]; j = i + 1; x = why = None
            while j < n:
                if df["open"].iloc[j] <= st: x, why = df["open"].iloc[j], "gap_stop"; break
                if df["low"].iloc[j] <= st: x, why = st, "stop"; break
                if c.iloc[j] > df["high"].iloc[j - 1]: x, why = c.iloc[j], "exit"; break
                if j - i >= p["hold"]: x, why = c.iloc[j], "time"; break
                j += 1
            if x is None: break
            out.append(rec(sym, "ibs", df.index[i], df.index[j], e, x, st, why)); i = j + 1
        else:
            i += 1
    return out


# ---------------- intraday (flat at the close) ----------------
def orb(sym, df, p, spy_ret=None):
    """Zarattini, Barbon & Aziz 2024 'A Profitable Day Trading Strategy for the U.S. Equity Market' (5m ORB on
    Stocks in Play): opening bar bullish + relative volume high -> long at next bar, stop = p x ATR14(daily) below,
    exit at the close. Adaptation: 15m opening bar (free data), long only, optional RS vs SPY on the opening bar."""
    out = []
    g = list(df.groupby("day"))
    vols = [x["volume"].iloc[0] for _, x in g]
    rng = [x["high"].max() - x["low"].min() for _, x in g]
    for k in range(14, len(g)):
        day, x = g[k]
        if len(x) < 3: continue
        b = x.iloc[0]
        rv = b["volume"] / (np.mean(vols[k - 14:k]) or np.nan)
        if not (b["close"] > b["open"] and rv >= p["rvol"]): continue
        if p.get("rs") and spy_ret is not None and (b["close"] / b["open"] - 1) <= spy_ret.get(day, 0): continue
        e = x["open"].iloc[1]; a = np.mean(rng[k - 14:k])
        st = e - p["stop_atr"] * a if p["stop_atr"] else b["low"]
        if st >= e: continue
        xx, why = x["close"].iloc[-1], "eod"
        for j in range(1, len(x)):
            if x["low"].iloc[j] <= st: xx, why = min(st, x["open"].iloc[j]), "stop"; break
        t = rec(sym, "orb", x.index[1], x.index[-1], e, xx, st, why); t["rv"] = rv; t["day"] = day
        out.append(t)
    return out


def vwap_trend(sym, df, p):
    """Zarattini & Aziz 2023 'VWAP: the Holy Grail for Day Trading Systems': long while price > session VWAP.
    Adaptation: 15m bars, enter on the first close above VWAP after the first hour if the day is above the open
    (trend filter), stop k x ATR(15m) below, exit on a close below VWAP or at the close."""
    out = []
    a15 = atr(df, 14)
    for day, x in df.groupby("day"):
        if len(x) < 8: continue
        tp = (x["high"] + x["low"] + x["close"]) / 3
        vw = (tp * x["volume"]).cumsum() / x["volume"].cumsum().replace(0, np.nan)
        pos = None
        for j in range(p["skip"], len(x) - 1):
            c = x["close"].iloc[j]
            if pos is None and c > vw.iloc[j] and x["close"].iloc[j - 1] <= vw.iloc[j - 1] and c > x["open"].iloc[0]:
                e = x["open"].iloc[j + 1]; st = e - p["k"] * a15.loc[x.index[j]]
                pos = (j + 1, e, st); continue
            if pos is not None:
                i0, e, st = pos
                if x["low"].iloc[j] <= st:
                    out.append(rec(sym, "vwap", x.index[i0], x.index[j], e, min(st, x["open"].iloc[j]), st, "stop")); pos = None; break
                if c < vw.iloc[j]:
                    out.append(rec(sym, "vwap", x.index[i0], x.index[j], e, c, st, "vwap")); pos = None; break
        if pos is not None:
            i0, e, st = pos
            out.append(rec(sym, "vwap", x.index[i0], x.index[-1], e, x["close"].iloc[-1], st, "eod"))
    return out


def gap_fill(sym, df, p, d):
    """Gap-fill (documented e.g. in Connors & Raschke 'Street Smarts' 1995 and many gap studies): in an uptrend
    (daily close > SMA200), an opening gap down of g..3 x g % tends to close toward the prior close.
    Adaptation: buy at the 2nd 15m bar open if the first bar closed up, target = prior close, stop k x daily ATR(10),
    flat at the close (no overnight)."""
    if d is None: return []
    c = d["close"]; s200 = c.rolling(200).mean().shift(); a = atr(d, 10).shift(); pc = c.shift()
    key = {t.tz_convert("America/New_York").date(): (pc.iloc[i], s200.iloc[i], a.iloc[i]) for i, t in enumerate(d.index)}
    out = []
    for day, x in df.groupby("day"):
        if day not in key or len(x) < 3: continue
        prev, sma, av = key[day]
        if not (prev == prev and sma == sma and av == av) or prev <= sma: continue
        o = x["open"].iloc[0]; gap = (o / prev - 1) * 100
        if not (-3 * p["g"] <= gap <= -p["g"]): continue
        if p["confirm"] and x["close"].iloc[0] <= x["open"].iloc[0]: continue
        e = x["open"].iloc[1]; st = e - p["k"] * av; tg = prev
        if not st < e < tg: continue
        xx, why = x["close"].iloc[-1], "eod"
        for j in range(1, len(x)):
            if x["low"].iloc[j] <= st: xx, why = min(st, x["open"].iloc[j]), "stop"; break
            if x["high"].iloc[j] >= tg: xx, why = max(tg, x["open"].iloc[j]), "fill"; break
        out.append(rec(sym, "gap", x.index[1], x.index[-1], e, xx, st, why))
    return out


def stats(ts, max_open=2):
    ts = sorted(ts, key=lambda t: t["in"])
    open_, taken = [], []
    for t in ts:
        open_ = [o for o in open_ if o["out"] > t["in"]]
        if len(open_) >= max_open or any(o["sym"] == t["sym"] for o in open_): continue
        open_.append(t); taken.append(t)
    if not taken: return {"n": 0}
    r = np.array([t["r"] for t in taken]); pc = np.array([t["pct"] for t in taken])
    eq = np.cumsum(r); dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], eq]))[1:] - eq))
    # unlevered equity: each trade uses 1/max_open of equity
    curve = np.cumprod(1 + pc / 100 / max_open); ddp = float(np.max(1 - curve / np.maximum.accumulate(curve)) * 100)
    return {"n": len(r), "wr": round(float((r > 0).mean()), 3), "avgR": round(float(r.mean()), 3),
            "ddR": round(dd, 1), "avg%": round(float(pc.mean()), 3), "ret%": round(float((curve[-1] - 1) * 100), 1),
            "dd%": round(ddp, 1), "pf": round(float(r[r > 0].sum() / -r[r <= 0].sum()), 2) if (r <= 0).any() else None}


def split(ts, cut):
    return stats([t for t in ts if t["in"] < cut]), stats([t for t in ts if t["in"] >= cut])


def _spy_regime():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app.regime import regime_series
    r = regime_series(daily("SPY")).dropna(subset=["trend", "vol"])
    lab = (r["trend"] + "/" + r["vol"])
    lab.index = lab.index.tz_convert("America/New_York").normalize()
    return lab


REG = None


def reg_at(ts):
    """Regime of the last COMPLETED day before the entry (entry at a daily close uses that day's own close
    only for its signal, the regime comes from the day before - no look-ahead)."""
    d = pd.Timestamp(ts).tz_convert("America/New_York").normalize()
    prev = REG[REG.index < d]
    return prev.iloc[-1] if len(prev) else None


def cells(ts, cut, min_n=15):
    out = {}
    for c in sorted({t["reg"] for t in ts if t["reg"]}):
        sel = [t for t in ts if t["reg"] == c]
        i, o = split(sel, cut)
        out[c] = {"is": i, "oos": o}
    return out


if __name__ == "__main__":
    REG = _spy_regime()
    syms = sorted({f.name.split("_")[0] for f in D.glob("*_1Day.csv.gz")})
    dd = {s: daily(s) for s in syms}; dd = {k: v for k, v in dd.items() if v is not None}
    if "--old-fees" not in sys.argv:
        dv = {s: float((d["close"] * d["volume"]).tail(250).median()) for s, d in dd.items()}
        top = sorted(dv, key=lambda s: -dv[s])[:30]
        SLIP_BY.update({s: (2.5 if s in top else 6.0) / 1e4 for s in dd})
    ii = {s: intra(s) for s in syms}; ii = {k: v for k, v in ii.items() if v is not None}
    spy = ii.get("SPY"); spy_ret = {d: x["close"].iloc[0] / x["open"].iloc[0] - 1 for d, x in spy.groupby("day")} if spy is not None else {}
    print(f"{len(dd)} daily symbols, {len(ii)} intraday symbols", file=sys.stderr)
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    grid = {
        "rsi2": [dict(rsi=r, k=k, hold=10) for r in (5, 10, 15) for k in (2.0, 3.0)],
        "ibs": [dict(ibs=b, k=k, hold=10) for b in (0.1, 0.2, 0.3) for k in (2.0, 3.0)],
        "orb": [dict(rvol=v, stop_atr=s, rs=rs) for v in (1.5, 2.5) for s in (0.1, 0.0) for rs in (False, True)]
               + [dict(rvol=1.0, stop_atr=s, rs=False, top=n) for s in (0.1, 0.0) for n in (2, 5)],
        "vwap": [dict(skip=s, k=k) for s in (4, 8) for k in (1.0, 2.0)],
        "gap": [dict(g=g, k=k, confirm=cf) for g in (0.5, 1.0) for k in (0.5, 1.0) for cf in (False, True)],
    }
    fn = {"rsi2": connors_rsi2, "ibs": ibs, "orb": lambda s, d, p: orb(s, d, p, spy_ret), "vwap": vwap_trend, "gap": lambda s, d, p: gap_fill(s, d, p, dd.get(s))}
    res = []
    for name, ps in grid.items():
        if only and name not in only: continue
        src, cut = (dd, OOS_DAILY) if name in ("rsi2", "ibs") else (ii, OOS_INTRA)
        for p in ps:
            ts = [t for s, d in src.items() for t in fn[name](s, d, p)]
            if name == "orb" and p.get("top"):          # paper: only the top-N "stocks in play" by relative volume
                by = {}
                for t in ts: by.setdefault(t["day"], []).append(t)
                ts = [t for v in by.values() for t in sorted(v, key=lambda t: -t["rv"])[:p["top"]]]
            i, o = split(ts, cut)
            for t in ts:
                t["reg"] = reg_at(t["in"])
            res.append({"strategy": name, "params": p, "is": i, "oos": o, "cells": cells(ts, cut)})
            print(name, p, "IS", i, "OOS", o, flush=True)
    json.dump(res, open("/tmp/stock_strats_new.json" if not SLIP_BY == {} else "/tmp/stock_strats.json", "w"),
              default=str, indent=1)
