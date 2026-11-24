"""ACTIVE TRADE MANAGEMENT (DECISIONS D92) - same rules for the main paper ledger and the shadow ledger.

All decisions use COMPLETE 15m bars (ATR14 / EMA / RSI) plus the live price; R = entry - INITIAL stop.
1. Stop-loss only tightens (DB triggers also forbid widening):
   * open profit >= +1.0 R  -> stop to breakeven + costs (entry fee + exit fee + exit slippage covered)
   * open profit >= +1.5 R  -> trail: max(breakeven, min(highest high since entry - 2.0 x ATR14, lowest low of the last
     5 complete 15m bars)); a move is applied only if it tightens by >= 0.1 R and stays >= 0.25 ATR below price.
2. Take-profit only shrinks, never above the original, never below price + 0.5 ATR, applied if it shrinks by >= 0.1 R:
   * momentum fades  (profit >= +0.5 R, last close < EMA9 and RSI14 < 50)      -> price + 1.0 ATR
   * volatility drop (ATR14 now < 0.5 x ATR14 at entry)                        -> price + 2.0 ATR
   * resistance / liquidity zone (pivot high, 2 bars each side, last 96 bars,
     between price + 0.5 ATR and the target)                                    -> pivot - 0.1 ATR
   * news sentiment turns (symbol news since entry, last 2 h, mean <= -0.25)    -> price + 1.0 ATR
3. Early exit (market sell with the normal cost model):
   * while net in profit: strong adverse news (hard negative catalyst, or mean sentiment <= -0.5 over >= 2 items)
     or momentum reversal (last 2 complete closes < EMA20 and RSI14 < 40)
   * while losing: ONLY a hard negative catalyst for the symbol published after entry
   * session end / time stop: unchanged simulator rules (stock EOD flatten, crypto max hold)
4. Never adds, never averages down (quantity is immutable in the DB).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.data.models import AssetClass
from app.execution.costs import CostModel
from app.technical.indicators import atr, ema, rsi, to_frame

HARD_NEGATIVE = {"bankruptcy", "exchange_hack", "exchange_delisting", "crypto_regulatory_negative", "guidance_cut",
                 "earnings_miss", "fda_rejection", "trial_failure"}


@dataclass(frozen=True)
class Rules:
    breakeven_r: float = 1.0
    trail_start_r: float = 1.5
    trail_atr_mult: float = 2.0
    swing_bars: int = 5
    min_step_r: float = 0.1
    stop_buffer_atr: float = 0.25
    tp_floor_atr: float = 0.5
    fade_min_r: float = 0.5
    vol_drop_ratio: float = 0.5
    pivot_lookback: int = 96
    news_hours: float = 2.0
    news_turn: float = -0.25
    news_strong: float = -0.5


RULES = Rules()


@dataclass
class Action:
    kind: str          # stop | target | exit
    rule: str
    new: float | None
    reason: str


def symbol_news(db, symbol: str, since: datetime, now: datetime) -> dict:
    from app.intelligence.catalysts import detect
    lo = max(since, now - timedelta(hours=RULES.news_hours))
    rows = db.conn.execute(
        "SELECT n.id, n.title, n.summary, COALESCE(n.published_ts, n.fetched_ts) AS t, "
        "(SELECT AVG(s.score) FROM sentiment_results s WHERE s.news_event_id=n.id AND s.symbol=?) AS score "
        "FROM news_events n WHERE n.tickers LIKE ? ORDER BY n.id DESC LIMIT 50", (symbol, f"%,{symbol},%")).fetchall()
    items, hard = [], []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["t"])
        except Exception:
            continue
        if not (lo <= t <= now):
            continue
        items.append(r)
        cats, _ = detect(f"{r['title']} {r['summary'] or ''}")
        hard += [f"{c.category}: {r['title'][:90]}" for c in cats if c.category in HARD_NEGATIVE and c.direction < 0]
    sc = [r["score"] for r in items if r["score"] is not None]
    return {"n": len(sc), "mean": (sum(sc) / len(sc)) if sc else None, "hard": hard}


def decide(pos: dict, bars15, price: float, news: dict, costs: CostModel, now: datetime) -> list[Action]:
    """Pure decision function. pos needs entry_price, stop_loss, take_profit, initial_stop, initial_target,
    quantity, entry_fee (USD), asset_class, opened_ts."""
    ac = AssetClass(pos["asset_class"])
    entry, stop, target = pos["entry_price"], pos["stop_loss"], pos["take_profit"]
    r_unit = entry - (pos.get("initial_stop") or stop)
    if r_unit <= 0 or not price:
        return []
    qty = pos["quantity"]
    fee_b = costs.fee_bps(ac) / 1e4
    entry_cost = pos["entry_fee"] / qty
    r_now = (price - entry) / r_unit
    net_now = costs.sell_fill(ac, price).price * (1 - fee_b) - entry - entry_cost
    df = to_frame(bars15 or [])
    out: list[Action] = []
    # ---- 3. early exits first
    hard = news.get("hard") or []
    strong = bool(hard) or (news.get("n", 0) >= 2 and (news.get("mean") or 0) <= RULES.news_strong)
    if net_now > 0 and strong:
        return [Action("exit", "adverse_news", None, f"in profit {r_now:+.2f} R; strong adverse news: "
                       + ("; ".join(hard[:2]) if hard else f"mean sentiment {news['mean']:.2f} over {news['n']} items"))]
    if net_now <= 0 and hard:
        return [Action("exit", "hard_negative_catalyst", None, f"losing {r_now:+.2f} R; hard negative catalyst: {hard[0]}")]
    if len(df) < 20:
        return out
    a = atr(df, 14)
    a_now = float(a.iloc[-1])
    if not a_now or a_now != a_now:
        return out
    e9, e20, rs = ema(df["close"], 9), ema(df["close"], 20), rsi(df["close"], 14)
    c = df["close"]
    if net_now > 0 and c.iloc[-1] < e20.iloc[-1] and c.iloc[-2] < e20.iloc[-2] and rs.iloc[-1] < 40:
        return [Action("exit", "momentum_reversal", None, f"in profit {r_now:+.2f} R; 2 closes < EMA20 "
                       f"({e20.iloc[-1]:.6g}) and RSI14 {rs.iloc[-1]:.1f} < 40")]
    # ---- 1. stop: tighten only
    new_stop, why = stop, None
    if r_now >= RULES.breakeven_r:
        be = entry + entry_cost
        for _ in range(4):               # exit at `be` (stop slippage + exit fee) must net >= entry + entry fee
            net = costs.stop_fill(ac, be, be).price * (1 - fee_b)
            be += (entry + entry_cost) - net
        new_stop, why = max(new_stop, be), f"+{r_now:.2f} R >= {RULES.breakeven_r} R: breakeven + costs {be:.6g}"
        if r_now >= RULES.trail_start_r:
            opened = datetime.fromisoformat(pos["opened_ts"])
            since = df[df.index >= opened]
            hh = max(float(since["high"].max()) if len(since) else price, price)
            chand = hh - RULES.trail_atr_mult * a_now
            swing = float(df["low"].iloc[-RULES.swing_bars:].min())
            trail = min(chand, swing)
            if trail > new_stop:
                new_stop, why = trail, (f"+{r_now:.2f} R trail: min(HH {hh:.6g} - {RULES.trail_atr_mult} ATR = {chand:.6g}, "
                                        f"{RULES.swing_bars}-bar swing low {swing:.6g})")
    new_stop = min(new_stop, price - RULES.stop_buffer_atr * a_now)
    if new_stop > stop and new_stop - stop >= RULES.min_step_r * r_unit:
        out.append(Action("stop", "breakeven" if "trail" not in (why or "") else "trail", new_stop, why))
    # ---- 2. target: reduce only
    floor = price + RULES.tp_floor_atr * a_now
    cands = []
    if r_now >= RULES.fade_min_r and c.iloc[-1] < e9.iloc[-1] and rs.iloc[-1] < 50:
        cands.append((price + a_now, "momentum_fade", f"close < EMA9 and RSI14 {rs.iloc[-1]:.1f} < 50"))
    a_entry = a[a.index < datetime.fromisoformat(pos["opened_ts"])]
    if len(a_entry) and a_entry.iloc[-1] == a_entry.iloc[-1] and a_now < RULES.vol_drop_ratio * float(a_entry.iloc[-1]):
        cands.append((price + 2 * a_now, "volatility_drop", f"ATR14 {a_now:.6g} < 0.5 x entry ATR {float(a_entry.iloc[-1]):.6g}"))
    h = df["high"].iloc[-RULES.pivot_lookback:]
    piv = [float(h.iloc[i]) for i in range(2, len(h) - 2)
           if h.iloc[i] == h.iloc[i - 2:i + 3].max() and floor < h.iloc[i] - 0.1 * a_now < target]
    if piv:
        p0 = min(piv)
        cands.append((p0 - 0.1 * a_now, "resistance", f"pivot high / liquidity zone {p0:.6g} below target"))
    if news.get("n", 0) >= 1 and (news.get("mean") or 0) <= RULES.news_turn:
        cands.append((price + a_now, "news_turn", f"symbol sentiment turned {news['mean']:.2f} ({news['n']} items)"))
    if cands:
        v, rule, why = min(cands)
        v = min(max(v, floor), target, pos.get("initial_target") or target)
        if target - v >= RULES.min_step_r * r_unit:
            out.append(Action("target", rule, v, why))
    return out


class TradeManager:
    def __init__(self, settings, db, mirror=None):
        self.s, self.db, self.mirror = settings, db, mirror
        self.costs = CostModel(settings)

    def _log(self, ledger, pos, act: Action, old, price, r_now, alpaca=None, now=None):
        self.db.insert("trade_adjustments", {
            "ts": now.isoformat(), "ledger": ledger, "position_id": pos["id"], "symbol": pos["symbol"], "kind": act.kind,
            "rule": act.rule, "old_value": old, "new_value": act.new, "price": price, "r_now": round(r_now, 4),
            "reason": act.reason, "alpaca_json": json.dumps(alpaca) if alpaca is not None else None})

    def apply(self, ledger: str, pos: dict, bars15, price: float, now: datetime, close_fn) -> list[dict]:
        """close_fn(pos, reason) closes the position on its ledger and returns a result dict."""
        table = "positions" if ledger == "main" else "shadow_positions"
        p = dict(pos)
        p["entry_fee"] = pos.get("entry_fee_usd") if ledger == "shadow" else pos["fees_usd"]
        news = symbol_news(self.db, pos["symbol"], datetime.fromisoformat(pos["opened_ts"]), now)
        acts = decide(p, bars15, price, news, self.costs, now)
        r_unit = pos["entry_price"] - (pos.get("initial_stop") or pos["stop_loss"])
        r_now = (price - pos["entry_price"]) / r_unit if r_unit > 0 else 0
        out = []
        for a in acts:
            if a.kind == "exit":
                res = close_fn(pos, f"mgmt_{a.rule}")
                al = res.pop("_alpaca", None) if isinstance(res, dict) else None
                self._log(ledger, pos, a, None, price, r_now, al, now)
                out.append({"kind": "exit", "rule": a.rule, "reason": a.reason, **(res or {})})
                break
            col = "stop_loss" if a.kind == "stop" else "take_profit"
            old = pos[col]
            self.db.conn.execute(f"UPDATE {table} SET {col}=? WHERE id=? AND status='open'", (a.new, pos["id"]))
            al = None
            if ledger == "main" and self.mirror is not None:
                al = self.mirror.adjust(pos["id"], pos["symbol"], pos["asset_class"], a.kind, a.new)
            self._log(ledger, pos, a, old, price, r_now, al, now)
            out.append({"kind": a.kind, "rule": a.rule, "old": old, "new": a.new, "reason": a.reason, "alpaca": al})
        return out
