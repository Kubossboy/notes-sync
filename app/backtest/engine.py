"""BACKTEST ENGINE - historical replay of the live strategy code with the live cost/fill model.

Causality: indicators are computed once on the full history with causal formulas
(tests/test_backtest.py proves compute_all(full)[:k] == compute_all(full[:k])); at bar i
the strategy only sees rows <= i (a trailing window, like the live scanner). A setup on the
close of bar i is entered at the OPEN of bar i+1 with CostModel.buy_fill (assumed half spread
+ slippage, no historical bid/ask); exits use the same stop/target/time rules as the paper
simulator (stop first if both are hit in one bar, gaps through the stop fill at the open).
Portfolio rules mirror the risk engine: 0.25 % risk of current equity via size_position,
max open positions, one per symbol, daily loss limit, consecutive-loss stop, symbol cooldown,
no-chasing, net R:R >= 2 at the actual fill, stock entry window and EOD flatten.

NOT reproducible historically (documented, never faked): news / sentiment / composite score
(no free historical news archive), order-book strategies (no historical L2), live liquidity
filters for IEX volume. Those strategies are reported as "not_backtestable".
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.config import Settings
from app.data.calendar import ET, regular_close_at, trading_day_of
from app.data.models import AssetClass
from app.execution.costs import CostModel
from app.risk.engine import size_position
from app.scanners.scoring import build_plan, cost_bps_per_side
from app.strategies.base import NoSetup, StrategyContext
from app.strategies.crypto import CRYPTO_STRATEGIES
from app.strategies.stock import STOCK_STRATEGIES
from app.technical.indicators import compute_all, support_resistance

NOT_BACKTESTABLE = {
    "news_momentum": "needs historical news/catalysts - no free historical news archive",
    "orderbook_imbalance": "needs historical L2 order books - not available from free sources",
    "liquidity_change": "needs historical L2 order books - not available from free sources",
}


@dataclass
class Candidate:
    symbol: str
    strategy: str
    variant: str
    bar_i: int
    signal_ts: pd.Timestamp
    entry: float
    stop: float
    target: float
    rr_net: float = 0.0
    gated: bool = True          # False = diagnostic only (failed the live net R:R gate)


def problem_kind(p: str) -> str:
    if p.startswith("net reward/risk"):
        return "net R:R below minimum" + (" (target capped by resistance)" if "capped" in p else "")
    return p.split("(")[0].strip()


@dataclass
class Trade:
    symbol: str
    strategy: str
    variant: str
    signal_ts: pd.Timestamp
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    exit_price: float
    stop: float
    target: float
    qty: float
    planned_risk: float
    fees: float
    pnl: float
    r_multiple: float
    exit_reason: str
    equity_before: float


# direction of "looser" (more setups) per parameter; ambiguous ones are never varied
LOOSER_IF_HIGHER = {"rsi_max", "max_minutes_after_open", "squeeze_pctile", "max_bid_drop_pct", "pullback_bars",
                    "lookback", "high_lookback"}
NOT_VARIED = {"target_r", "or_minutes", "squeeze_lookback", "depth_pct", "min_history"}
INT_FLOOR = {"min_touches": 2, "bars": 1, "pullback_bars": 1, "lookback": 2, "high_lookback": 2,
             "min_bars_below": 1, "donchian": 5}


def variants_for(params: dict, step: float = 0.2) -> dict[str, dict]:
    """default + 'loose' + 'strict': every unambiguous numeric threshold moved 20 % in the direction that
    admits more (loose) / fewer (strict) setups; integer params move by at least 1 (parameter sensitivity)."""
    out = {"default": dict(params)}
    for name, sign in (("loose", 1), ("strict", -1)):
        v = {}
        for k, x in params.items():
            if k in NOT_VARIED or isinstance(x, bool) or not isinstance(x, (int, float)):
                v[k] = x
                continue
            d = sign if k in LOOSER_IF_HIGHER else -sign          # +1 = increase the value
            y = x * (1 + d * step)
            if float(x).is_integer() and k not in ("min_rvol", "min_roc", "close_in_range", "min_imbalance",
                                                     "min_confidence"):
                y = int(round(y))
                if y == int(x):
                    y = int(x) + d
                y = max(y, INT_FLOOR.get(k, 1))
            v[k] = y
        out[name] = v
    return out


class Backtester:
    def __init__(self, settings: Settings, cost_multiplier: float = 1.0):
        self.s = settings
        if cost_multiplier != 1.0:
            ex = settings.execution
            settings = settings.model_copy(update={"execution": ex.model_copy(update={
                "crypto_fee_bps": ex.crypto_fee_bps * cost_multiplier,
                "stock_fee_bps": ex.stock_fee_bps * cost_multiplier,
                "slippage_bps_crypto": ex.slippage_bps_crypto * cost_multiplier,
                "slippage_bps_stock": ex.slippage_bps_stock * cost_multiplier,
                "assumed_half_spread_bps_crypto": ex.assumed_half_spread_bps_crypto * cost_multiplier,
                "assumed_half_spread_bps_stock": ex.assumed_half_spread_bps_stock * cost_multiplier})})
        self.cs = settings                       # settings incl. (possibly stressed) costs
        self.costs = CostModel(settings)

    # ------------------------------------------------------------------ signal generation
    def prepare(self, raw: pd.DataFrame, ac: AssetClass) -> pd.DataFrame:
        base = raw[raw["session"] == "regular"] if ac == AssetClass.STOCK and "session" in raw else raw
        return compute_all(base, ac)

    def candidates(self, ind: pd.DataFrame, symbol: str, ac: AssetClass, tf: str,
                   strategies: dict[str, dict[str, dict]], window: int = 400, warmup: int = 250,
                   level_every: int = 1, diag_min_rr: float | None = None) -> list[Candidate]:
        """strategies: {name: {variant: params}}. Evaluates every bar i >= warmup on rows <= i.

        self.diag counts raw setups and plan rejections per (strategy, variant). With diag_min_rr,
        setups whose ONLY problem is the net R:R gate and whose net R:R >= diag_min_rr are also
        returned with gated=False (diagnostic 'what if the gate were looser' - never used for verdicts)."""
        from collections import Counter
        self.diag: dict = {}
        table = STOCK_STRATEGIES if ac == AssetClass.STOCK else CRYPTO_STRATEGIES
        cost = cost_bps_per_side(self.cs, ac)
        out: list[Candidate] = []
        n = len(ind)
        levels: list[dict] = []
        for i in range(warmup, n - 1):                      # last bar has no next open to enter at
            d = ind.iloc[max(0, i - window + 1): i + 1]
            if (i - warmup) % level_every == 0:
                levels = support_resistance(d)
            ctx = StrategyContext(symbol, ac, tf, d, None, levels, None, [], d.index[-1].to_pydatetime(), cost)
            for name, vs in strategies.items():
                fn = table[name]
                for vname, params in vs.items():
                    try:
                        st = fn(ctx, params)
                    except NoSetup:
                        continue
                    dg = self.diag.setdefault((name, vname), Counter())
                    dg["raw_setups"] += 1
                    plan = build_plan(st, ctx, self.cs)
                    if plan.problems:
                        for pr in plan.problems:
                            dg["rejected: " + problem_kind(pr)] += 1
                        only_rr = all(pr.startswith("net reward/risk") for pr in plan.problems)
                        if diag_min_rr is not None and only_rr and plan.reward_risk_net >= diag_min_rr:
                            out.append(Candidate(symbol, name, vname, i, d.index[-1], plan.entry, plan.stop,
                                                 plan.target, plan.reward_risk_net, gated=False))
                        continue
                    dg["plan_ok"] += 1
                    out.append(Candidate(symbol, name, vname, i, d.index[-1], plan.entry, plan.stop, plan.target,
                                         plan.reward_risk_net))
        return out

    # ------------------------------------------------------------------ exits
    def exit_path(self, ind: pd.DataFrame, c: Candidate, ac: AssetClass, tf_s: int):
        """Entry at next bar open; walk forward to the exit. Returns (entry_fill, exit_i, exit_fill, reason)
        or None if the entry is not allowed (gap past stop / chasing / stock entry window)."""
        j = c.bar_i + 1
        o, h, l, cl = (ind[k].to_numpy() for k in ("open", "high", "low", "close"))
        idx = ind.index
        fill = self.costs.buy_fill(ac, float(o[j]))
        r_unit = c.entry - c.stop
        if fill.price <= c.stop or (fill.price - c.entry) / r_unit > self.s.risk.max_entry_drift_r:
            return None
        if ac == AssetClass.STOCK:
            close = regular_close_at(idx[j].to_pydatetime())
            if close is None or idx[j].to_pydatetime() + timedelta(
                    minutes=self.s.risk.stock_no_new_entries_minutes_before_close) >= close:
                return None
        entry_t = idx[j]
        day_close = regular_close_at(entry_t.to_pydatetime()) if ac == AssetClass.STOCK else None
        max_hold = timedelta(hours=self.s.risk.crypto_max_hold_hours)
        k = j
        while k < len(ind):
            if l[k] <= c.stop:
                return fill, k, self.costs.stop_fill(ac, c.stop, float(o[k])), "stop_loss"
            if h[k] > c.target:
                return fill, k, self.costs.target_fill(ac, c.target, float(o[k])), "take_profit"
            bar_end = idx[k].to_pydatetime() + timedelta(seconds=tf_s)
            if ac == AssetClass.STOCK:
                nxt_day = k + 1 < len(ind) and trading_day_of(idx[k + 1].to_pydatetime()) != trading_day_of(idx[k].to_pydatetime())
                if bar_end >= day_close - timedelta(minutes=self.s.risk.stock_flatten_minutes_before_close) or nxt_day:
                    return fill, k, self.costs.sell_fill(ac, float(cl[k])), "eod_flatten"
            elif bar_end - entry_t.to_pydatetime() >= max_hold:
                return fill, k, self.costs.sell_fill(ac, float(cl[k])), "max_hold_time"
            k += 1
        return fill, len(ind) - 1, self.costs.sell_fill(ac, float(cl[-1])), "end_of_data"

    # ------------------------------------------------------------------ portfolio replay
    def simulate(self, data: dict[str, pd.DataFrame], cands: list[Candidate], ac: AssetClass, tf_s: int,
                 start_equity: float | None = None) -> list[Trade]:
        """Chronological replay of one strategy/variant across symbols with live portfolio rules."""
        r = self.s.risk
        equity0 = start_equity or self.s.portfolio.starting_equity_usd
        realized = 0.0
        open_pos: list[dict] = []
        trades: list[Trade] = []
        day_start: dict = {}
        losses_today: dict = {}
        last_loss: dict[str, pd.Timestamp] = {}
        closes = {s: (df.index, df["close"].to_numpy()) for s, df in data.items()}

        def mark(sym, t):
            ix, c = closes[sym]
            p = ix.searchsorted(t, side="right") - 1
            return float(c[max(p, 0)])

        for c in sorted(cands, key=lambda x: (x.signal_ts, x.symbol)):
            ind = data[c.symbol]
            path = self.exit_path(ind, c, ac, tf_s)
            if path is None:
                continue
            fill, exit_i, xfill, reason = path
            t = ind.index[c.bar_i + 1]
            # close positions that exited before this entry
            for p in [p for p in open_pos if p["exit_ts"] <= t]:
                realized += p["trade"].pnl
                open_pos.remove(p)
                day = trading_day_of(p["exit_ts"].to_pydatetime())
                if p["trade"].pnl < 0:
                    losses_today[day] = losses_today.get(day, 0) + 1
                    last_loss[p["trade"].symbol] = p["exit_ts"]
                else:
                    losses_today[day] = 0
            unreal = sum((mark(p["trade"].symbol, t) - p["trade"].entry_price) * p["trade"].qty for p in open_pos)
            open_cost = sum(p["trade"].entry_price * p["trade"].qty for p in open_pos)
            equity = equity0 + realized + unreal
            cash = equity0 + realized - open_cost
            day = trading_day_of(t.to_pydatetime())
            if day not in day_start:
                day_start[day] = equity
            limit = day_start[day] * r.max_daily_loss_pct / 100
            dloss = max(0.0, day_start[day] - equity)
            if (len(open_pos) >= r.max_open_positions or any(p["trade"].symbol == c.symbol for p in open_pos)
                    or dloss >= limit or losses_today.get(day, 0) >= r.max_consecutive_losses_per_day
                    or (c.symbol in last_loss and (t - last_loss[c.symbol]).total_seconds() / 60
                        < r.symbol_cooldown_minutes_after_loss)):
                continue
            z = size_position(self.costs, self.cs, ac, fill.price, c.stop, c.target, equity, cash, limit - dloss)
            if z.qty <= 0 or z.notional < self.s.execution.min_notional_usd or z.rr_net is None \
                    or z.rr_net < r.min_reward_risk:
                continue
            fee_in = self.costs.fee(z.qty * fill.price, ac)
            fee_out = self.costs.fee(z.qty * xfill.price, ac)
            pnl = (xfill.price - fill.price) * z.qty - fee_in - fee_out
            tr = Trade(c.symbol, c.strategy, c.variant, c.signal_ts, t, ind.index[exit_i], fill.price, xfill.price,
                       c.stop, c.target, z.qty, z.planned_risk, fee_in + fee_out, pnl,
                       pnl / z.planned_risk if z.planned_risk else 0.0, reason, equity)
            trades.append(tr)
            open_pos.append({"trade": tr, "exit_ts": ind.index[exit_i]})
        return trades


# ---------------------------------------------------------------------- metrics
def metrics(trades: list[Trade], start_equity: float = 1000.0, periods_per_year: int = 252) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    pnl = np.array([t.pnl for t in trades])
    rs = np.array([t.r_multiple for t in trades])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    order = sorted(trades, key=lambda t: t.exit_ts)
    eq = start_equity + np.cumsum([t.pnl for t in order])
    peak = np.maximum.accumulate(np.r_[start_equity, eq])[1:]
    dd = float(((peak - eq) / peak).max() * 100)
    daily = pd.Series([t.pnl for t in order], index=pd.DatetimeIndex([t.exit_ts for t in order])).groupby(
        lambda x: x.date()).sum()
    sharpe = float(daily.mean() / daily.std() * math.sqrt(periods_per_year)) if len(daily) > 2 and daily.std() > 0 else None
    monthly = pd.Series([t.pnl for t in order], index=pd.DatetimeIndex([t.exit_ts for t in order])).groupby(
        lambda x: (x.year, x.month)).sum()
    return {
        "trades": n, "win_rate": round(float((pnl > 0).mean()), 4),
        "profit_factor": round(float(gross_win / gross_loss), 3) if gross_loss > 0 else None,
        "expectancy_r": round(float(rs.mean()), 4), "expectancy_usd": round(float(pnl.mean()), 4),
        "avg_win_usd": round(float(wins.mean()), 4) if len(wins) else None,
        "avg_loss_usd": round(float(losses.mean()), 4) if len(losses) else None,
        "net_pnl_usd": round(float(pnl.sum()), 4), "net_return_pct": round(float(pnl.sum() / start_equity * 100), 4),
        "max_drawdown_pct": round(dd, 4), "sharpe_daily": None if sharpe is None else round(sharpe, 3),
        "positive_months_pct": round(float((monthly > 0).mean() * 100), 1) if len(monthly) else None,
        "avg_hold_minutes": round(float(np.mean([(t.exit_ts - t.entry_ts).total_seconds() / 60 for t in trades])), 1),
        "fees_usd": round(float(sum(t.fees for t in trades)), 4),
        "exit_reasons": pd.Series([t.exit_reason for t in trades]).value_counts().to_dict(),
    }
