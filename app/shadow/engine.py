"""SHADOW FORWARD TEST (DECISIONS D91) - how strategies "learn" while paper trading.

Every live signal with valid long levels - from EVERY strategy, including ones that failed backtest
validation or are unavailable - opens a simulated trade on a separate, append-only shadow ledger
(`shadow_positions` -> `shadow_trades`) at the real current quote with the same cost model, stop and
target as the plan. It never touches the $1,000 portfolio, the risk engine, positions or Alpaca.

"Learning" = evidence-based promotion / demotion only (no parameter optimisation):
  promote  shadow -> eligible : >= shadow_min_trades closed shadow trades and, over the last
                                shadow_window trades, PF >= shadow_promote_pf, expectancy R > 0 after
                                costs, max drawdown <= validation.max_drawdown_pct of starting equity
  demote   eligible -> shadow : rolling-window PF < shadow_demote_pf
Each decision is appended to `strategy_status_log`. An eligible strategy may trade in the main paper
ledger, still under every M5 risk limit (quant state `shadow_promoted`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.data.calendar import regular_close_at
from app.data.models import AssetClass
from app.execution.costs import CostModel


def _table_exists(db, name: str) -> bool:
    return db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def window_stats(trades: list[dict]) -> dict:
    pnl = [t["pnl_usd"] for t in trades]
    rs = [t["r_multiple"] for t in trades]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    gl = -sum(losses)
    pf = (sum(wins) / gl) if gl > 0 else (None if not wins else float("inf"))
    eq = peak = dd = 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(trades), "win_rate": (len(wins) / len(pnl)) if pnl else None, "profit_factor": pf,
            "expectancy_r": (sum(rs) / len(rs)) if rs else None, "max_drawdown_usd": round(dd, 4),
            "net_pnl_usd": round(sum(pnl), 4)}


class ShadowEngine:
    def __init__(self, settings, db):
        self.s, self.db = settings, db
        self.v = settings.validation
        self.costs = CostModel(settings)

    @property
    def ready(self) -> bool:
        return self.v.shadow_enabled and _table_exists(self.db, "shadow_positions")

    def _risk_budget(self) -> float:
        return self.s.portfolio.starting_equity_usd * self.s.risk.max_risk_per_trade_pct / 100

    # ------------------------------------------------------------------ entries
    def open_from_signals(self, run_ids: list[int], quote_fn, now: datetime) -> list[dict]:
        if not self.ready or not run_ids:
            return []
        qm = ",".join("?" * len(run_ids))
        rows = self.db.conn.execute(
            f"SELECT * FROM signals WHERE scan_run_id IN ({qm}) AND direction='long' AND entry_price > 0 "
            f"AND stop_loss > 0 AND take_profit > entry_price AND stop_loss < entry_price", run_ids).fetchall()
        out = []
        for r in rows:
            if self.db.conn.execute("SELECT 1 FROM shadow_positions WHERE signal_id=? OR (strategy=? AND symbol=? "
                                    "AND status='open')", (r["id"], r["strategy"], r["symbol"])).fetchone():
                continue
            try:
                q = quote_fn(r["symbol"], now)
            except Exception:
                q = None
            if q is None or not q.price:
                continue
            ac = AssetClass(r["asset_class"])
            fill = self.costs.buy_fill(ac, q.price, q.bid, q.ask)
            if fill.price <= r["stop_loss"]:
                continue
            qty = self._risk_budget() / (fill.price - r["stop_loss"])
            fee = self.costs.fee(qty * fill.price, ac, self.costs.maker_entry(ac))
            self.db.insert("shadow_positions", {
                "opened_ts": now.isoformat(), "signal_id": r["id"], "symbol": r["symbol"], "asset_class": ac.value,
                "strategy": r["strategy"], "timeframe": r["timeframe"], "validation_status": r["validation_status"],
                "entry_price": fill.price, "stop_loss": r["stop_loss"], "take_profit": r["take_profit"], "initial_stop": r["stop_loss"], "initial_target": r["take_profit"],
                "quantity": qty, "planned_risk_usd": self._risk_budget(), "entry_fee_usd": fee, "mark_price": fill.price})
            out.append({"symbol": r["symbol"], "strategy": r["strategy"], "entry": fill.price})
        return out

    # ------------------------------------------------------------------ management
    def manage(self, bars_fn, now: datetime, bars15_fn=None) -> list[dict]:
        if not self.ready:
            return []
        out = []
        for pos in [dict(r) for r in self.db.conn.execute("SELECT * FROM shadow_positions WHERE status='open'")]:
            try:
                bars = bars_fn(pos)
                res = self._manage_one(pos, bars, now)
                if res is None and bars15_fn is not None and bars:       # D92: same management as the main ledger
                    from app.execution.management import TradeManager
                    cur = dict(self.db.conn.execute("SELECT * FROM shadow_positions WHERE id=?", (pos["id"],)).fetchone())
                    last = cur["mark_price"] or bars[-1].close
                    ac = AssetClass(cur["asset_class"])
                    adj = TradeManager(self.s, self.db).apply(
                        "shadow", cur, bars15_fn(cur["symbol"]), last, now,
                        lambda p, why: self._close(p, self.costs.sell_fill(ac, last).price, why, now))
                    res = next((a for a in adj if a["kind"] == "exit"), None)
                if res:
                    out.append(res)
            except Exception as e:                      # shadow problems never affect the main ledger
                self.db.log_error("shadow.manage", e)
        if out:
            self.evaluate(now)
        return out

    def _manage_one(self, pos: dict, bars, now: datetime) -> dict | None:
        ac = AssetClass(pos["asset_class"])
        opened = datetime.fromisoformat(pos["opened_ts"])
        after = datetime.fromisoformat(pos["last_managed_bar"]) if pos["last_managed_bar"] else None
        last_complete = None
        for b in sorted(bars or [], key=lambda x: x.open_time):
            if b.open_time < opened or (after is not None and b.open_time <= after):
                continue
            if b.low <= pos["stop_loss"]:
                return self._close(pos, self.costs.stop_fill(ac, pos["stop_loss"], b.open).price, "stop_loss", now)
            if b.high > pos["take_profit"]:
                return self._close(pos, self.costs.target_fill(ac, pos["take_profit"], b.open).price, "take_profit", now)
            if b.is_complete:
                last_complete = b.open_time
        last = bars[-1].close if bars else None
        reason = None
        if ac == AssetClass.STOCK:
            close, opened_close = regular_close_at(now), regular_close_at(opened)
            if close is None or (opened_close and now >= opened_close - timedelta(
                    minutes=self.s.risk.stock_flatten_minutes_before_close)):
                reason = "eod_flatten"
        elif now - opened >= timedelta(hours=self.s.risk.crypto_max_hold_hours):
            reason = "max_hold_time"
        if reason and last:
            return self._close(pos, self.costs.sell_fill(ac, last).price, reason, now)
        if last:
            self.db.conn.execute("UPDATE shadow_positions SET mark_price=?, last_managed_bar=COALESCE(?, last_managed_bar) "
                                 "WHERE id=? AND status='open'",
                                 (last, last_complete.isoformat() if last_complete else None, pos["id"]))
        return None

    def _close(self, pos: dict, px: float, reason: str, now: datetime) -> dict:
        ac = AssetClass(pos["asset_class"])
        exit_fee = self.costs.fee(pos["quantity"] * px, ac, reason == "take_profit" and self.costs.maker_entry(ac))
        fees = pos["entry_fee_usd"] + exit_fee
        pnl = (px - pos["entry_price"]) * pos["quantity"] - fees
        r = pnl / pos["planned_risk_usd"]
        ts = now.isoformat()
        with self.db.transaction():
            self.db.conn.execute("UPDATE shadow_positions SET status='closed', closed_ts=?, exit_price=?, exit_reason=?, "
                                 "pnl_usd=?, r_multiple=?, mark_price=? WHERE id=? AND status='open'",
                                 (ts, px, reason, round(pnl, 8), round(r, 6), px, pos["id"]))
            self.db.insert("shadow_trades", {
                "ts": ts, "shadow_position_id": pos["id"], "signal_id": pos["signal_id"], "symbol": pos["symbol"],
                "asset_class": ac.value, "strategy": pos["strategy"], "opened_ts": pos["opened_ts"], "closed_ts": ts,
                "entry_price": pos["entry_price"], "exit_price": px, "quantity": pos["quantity"],
                "planned_risk_usd": pos["planned_risk_usd"], "fees_usd": round(fees, 8), "pnl_usd": round(pnl, 8),
                "r_multiple": round(r, 6), "exit_reason": reason})
        return {"symbol": pos["symbol"], "strategy": pos["strategy"], "reason": reason, "pnl": round(pnl, 4), "r": round(r, 3)}

    # ------------------------------------------------------------------ promotion / demotion
    def trades(self, strategy: str, ac: str) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute(
            "SELECT * FROM shadow_trades WHERE strategy=? AND asset_class=? ORDER BY closed_ts, id", (strategy, ac))]

    def status(self, strategy: str, ac: str) -> str:
        if not _table_exists(self.db, "strategy_status_log"):
            return "shadow"
        r = self.db.conn.execute("SELECT to_status FROM strategy_status_log WHERE strategy=? AND asset_class=? "
                                 "ORDER BY id DESC LIMIT 1", (strategy, ac)).fetchone()
        return r["to_status"] if r else "shadow"

    def decide(self, strategy: str, ac: str) -> tuple[str, str, dict, dict]:
        """(current, proposed, all-time stats, window stats) - pure function of the shadow ledger."""
        allt = self.trades(strategy, ac)
        win = allt[-self.v.shadow_window:]
        ws, st = window_stats(win), window_stats(allt)
        cur = self.status(strategy, ac)
        dd_limit = self.s.portfolio.starting_equity_usd * self.v.max_drawdown_pct / 100
        pf = ws["profit_factor"]
        if cur == "shadow":
            ok = (len(allt) >= self.v.shadow_min_trades and pf is not None and pf >= self.v.shadow_promote_pf
                  and (ws["expectancy_r"] or 0) > 0 and ws["max_drawdown_usd"] <= dd_limit)
            return cur, ("eligible" if ok else "shadow"), st, ws
        bad = pf is not None and pf < self.v.shadow_demote_pf
        return cur, ("shadow" if bad else "eligible"), st, ws

    def evaluate(self, now: datetime | None = None) -> list[dict]:
        if not self.ready:
            return []
        now = now or datetime.now(timezone.utc)
        changes = []
        for r in self.db.conn.execute("SELECT DISTINCT strategy, asset_class FROM shadow_trades").fetchall():
            cur, new, st, ws = self.decide(r["strategy"], r["asset_class"])
            if new == cur:
                continue
            pf = ws["profit_factor"]
            reason = (f"{'PROMOTED' if new == 'eligible' else 'DEMOTED'}: last {ws['n']} shadow trades PF "
                      f"{'inf' if pf == float('inf') else round(pf or 0, 3)}, expectancy {round(ws['expectancy_r'] or 0, 3)} R, "
                      f"max DD ${ws['max_drawdown_usd']:.2f} ({st['n']} total)")
            self.db.insert("strategy_status_log", {
                "ts": now.isoformat(), "strategy": r["strategy"], "asset_class": r["asset_class"], "from_status": cur,
                "to_status": new, "n_trades": st["n"], "window_n": ws["n"],
                "profit_factor": None if pf in (None, float("inf")) else round(pf, 4),
                "expectancy_r": ws["expectancy_r"], "max_drawdown_usd": ws["max_drawdown_usd"], "reason": reason})
            self.db.log_event("strategy_" + ("promoted" if new == "eligible" else "demoted"), "shadow",
                              f"{r['asset_class']}/{r['strategy']}: {reason}", "warning")
            changes.append({"strategy": r["strategy"], "asset_class": r["asset_class"], "to": new, "reason": reason})
        return changes

    def summary(self, strategies: dict[str, list[str]]) -> list[dict]:
        rows = []
        ready = _table_exists(self.db, "shadow_trades")
        for ac, names in strategies.items():
            for name in names:
                cur, _, st, ws = self.decide(name, ac) if ready else ("shadow", None, window_stats([]), window_stats([]))
                open_n = self.db.conn.execute("SELECT COUNT(*) FROM shadow_positions WHERE status='open' AND strategy=? "
                                              "AND asset_class=?", (name, ac)).fetchone()[0] if ready else 0
                rows.append({"asset": ac, "strategy": name, "status": cur, "n": st["n"], "target": self.v.shadow_min_trades,
                             "open": open_n, "window_pf": ws["profit_factor"], "window_n": ws["n"],
                             "win_rate": ws["win_rate"], "expectancy_r": ws["expectancy_r"],
                             "max_drawdown_usd": ws["max_drawdown_usd"], "net_pnl_usd": st["net_pnl_usd"]})
        return rows


def strategy_names() -> dict[str, list[str]]:
    from app.strategies.crypto import CRYPTO_STRATEGIES
    from app.strategies.stock import STOCK_STRATEGIES
    return {"crypto": list(CRYPTO_STRATEGIES), "stock": list(STOCK_STRATEGIES)}
