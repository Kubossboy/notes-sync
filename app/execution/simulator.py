"""INTERNAL PAPER SIMULATOR: order submission, fills, positions, stop-loss and
take-profit management, time exits, mark-to-market. Paper only - this module has
no network code for orders at all. Every step is journaled (order_requests ->
simulated_fills -> positions -> equity_snapshots); fills are immutable.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.core.logging_setup import get_logger
from app.core.safety import SafeState
from app.data.calendar import regular_close_at, trading_day_of
from app.data.models import AssetClass, Candle
from app.database.db import Database
from app.execution.costs import CostModel, Fill
from app.portfolio.ledger import Ledger
from app.risk.leverage import bankrupt_price, liq_price
from app.risk.engine import MarketQuote, Proposal, RiskDecision

log = get_logger("execution")
PROVIDER = "internal_sim"


class DuplicateOrder(RuntimeError):
    pass


class PaperSimulator:
    def __init__(self, settings: Settings, db: Database, safe: SafeState, ledger: Ledger | None = None):
        self.s, self.db, self.safe = settings, db, safe
        self.costs = CostModel(settings)
        self.ledger = ledger or Ledger(settings, db)

    # ------------------------------------------------------------------ entries
    def open_position(self, p: Proposal, d: RiskDecision, q: MarketQuote, now: datetime) -> dict:
        assert self.s.execution.mode == "paper"
        if not d.approved:
            raise ValueError("refusing to execute a trade the risk engine did not approve")
        if not self.safe.trading_allowed():
            raise RuntimeError("SAFE STATE active - no new simulated trades")
        ac = p.asset_class
        fill = self.costs.buy_fill(ac, q.price, q.bid, q.ask)
        qty = d.quantity
        notional = qty * fill.price
        mk = self.costs.maker_entry(ac)
        fee = self.costs.fee(notional, ac, mk)
        coid = f"atlas-entry-sig{p.signal_id}" if p.signal_id is not None else f"atlas-entry-{d.evaluation_id}"
        ts = now.isoformat()
        try:
            with self.db.transaction():
                oid = self.db.insert("order_requests", {
                    "ts": ts, "client_order_id": coid, "signal_id": p.signal_id, "symbol": p.symbol,
                    "asset_class": ac.value, "side": "buy", "order_type": "limit" if mk else "market", "quantity": qty,
                    "stop_loss": p.stop, "take_profit": p.target, "position_size_usd": notional,
                    "planned_risk_usd": d.planned_risk_usd, "execution_mode": "paper", "provider": PROVIDER,
                    "status": "submitted", "purpose": "entry", "evaluation_id": d.evaluation_id,
                    "reference_price": fill.reference, "reason": f"{p.strategy} signal {p.signal_id}"})
                self.db.insert("simulated_fills", {
                    "ts": ts, "order_id": oid, "fill_price": fill.price, "quantity": qty, "fee_usd": fee,
                    "slippage_usd": fill.slippage_per_unit * qty, "reference_price": fill.reference,
                    "assumptions": fill.assumption + f"; fee {self.costs.fee_bps(ac, mk)}bps"})
                pid = self.db.insert("positions", {
                    "opened_ts": ts, "symbol": p.symbol, "asset_class": ac.value, "strategy": p.strategy,
                    "direction": "long", "quantity": qty, "entry_price": fill.price, "stop_loss": p.stop,
                    "take_profit": p.target, "fees_usd": fee, "slippage_usd": fill.slippage_per_unit * qty,
                    "unrealized_pnl_usd": -fee, "status": "open", "entry_order_id": oid,
                    "signal_id": p.signal_id, "planned_risk_usd": d.planned_risk_usd, "initial_stop": p.stop, "initial_target": p.target,
                    "leverage": p.leverage, "margin_usd": notional / p.leverage,
                    "liquidation_price": liq_price(fill.price, p.leverage, self.s.execution.maintenance_margin_pct),
                    "mark_price": fill.price, "mark_ts": ts, "mfe_price": fill.price, "mae_price": fill.price,
                    "trading_day": trading_day_of(now).isoformat(), "validation_status": p.validation_status})
                self.db.conn.execute("UPDATE order_requests SET status='filled', filled_ts=?, avg_fill_price=?, "
                                     "position_id=? WHERE id=?", (ts, fill.price, pid, oid))
                if p.signal_id is not None:
                    self.db.conn.execute("UPDATE signals SET status='executed' WHERE id=?", (p.signal_id,))
                    self.db.insert("strategy_decisions", {
                        "ts": ts, "signal_id": p.signal_id, "decision": "trade",
                        "reason": f"risk approved ({d.evaluation_id}); paper fill {qty} @ {fill.price:.6g}",
                        "details_json": json.dumps({"position_id": pid, "order_id": oid,
                                                    "planned_risk_usd": d.planned_risk_usd, "rr_net": d.rr_net})})
        except Exception as e:
            if "UNIQUE" in str(e):
                raise DuplicateOrder(f"duplicate entry blocked: {coid} / open position in {p.symbol}") from e
            raise
        self.db.log_event("paper_entry", "execution", f"{p.symbol} {qty} @ {fill.price:.6g} stop {p.stop:.6g} "
                                                      f"target {p.target:.6g} risk ${d.planned_risk_usd:.2f}")
        return {"position_id": pid, "order_id": oid, "client_order_id": coid, "quantity": qty,
                "fill_price": fill.price, "fee": fee, "assumption": fill.assumption}

    # ------------------------------------------------------------------ exits
    def close_position(self, pos: dict, fill: Fill, reason: str, now: datetime) -> dict:
        ac = AssetClass(pos["asset_class"])
        qty = pos["quantity"]
        mk = reason == "take_profit" and self.costs.maker_entry(ac)     # D101: TP limit = maker, rest taker
        fee = self.costs.fee(qty * fill.price, ac, mk)
        coid = f"atlas-exit-pos{pos['id']}"
        ts = now.isoformat()
        with self.db.transaction():
            cur = self.db.conn.execute("SELECT status FROM positions WHERE id=?", (pos["id"],)).fetchone()
            if cur["status"] != "open":
                raise DuplicateOrder(f"position {pos['id']} already closed")
            oid = self.db.insert("order_requests", {
                "ts": ts, "client_order_id": coid, "signal_id": pos["signal_id"], "symbol": pos["symbol"],
                "asset_class": ac.value, "side": "sell",
                "order_type": {"stop_loss": "stop", "take_profit": "limit"}.get(reason, "market"),
                "quantity": qty, "stop_loss": pos["stop_loss"], "take_profit": pos["take_profit"],
                "position_size_usd": qty * fill.price, "execution_mode": "paper", "provider": PROVIDER,
                "status": "submitted", "purpose": "exit", "position_id": pos["id"],
                "reference_price": fill.reference, "reason": reason})
            self.db.insert("simulated_fills", {
                "ts": ts, "order_id": oid, "fill_price": fill.price, "quantity": qty, "fee_usd": fee,
                "slippage_usd": fill.slippage_per_unit * qty, "reference_price": fill.reference,
                "assumptions": fill.assumption + f"; fee {self.costs.fee_bps(ac)}bps"})
            fees = pos["fees_usd"] + fee
            pnl = (fill.price - pos["entry_price"]) * qty - fees
            r_mult = pnl / pos["planned_risk_usd"] if pos["planned_risk_usd"] else None
            self.db.conn.execute(
                "UPDATE positions SET status='closed', closed_ts=?, exit_price=?, fees_usd=?, slippage_usd=?, "
                "realized_pnl_usd=?, unrealized_pnl_usd=0, exit_reason=?, exit_order_id=?, r_multiple=?, "
                "mark_price=?, mark_ts=?, trading_day_closed=? WHERE id=?",
                (ts, fill.price, fees, pos["slippage_usd"] + fill.slippage_per_unit * qty, round(pnl, 8), reason,
                 oid, None if r_mult is None else round(r_mult, 4), fill.price, ts,
                 trading_day_of(now).isoformat(), pos["id"]))
            self.db.conn.execute("UPDATE order_requests SET status='filled', filled_ts=?, avg_fill_price=? "
                                 "WHERE id=?", (ts, fill.price, oid))
            if pos.get("validation_status") == "forward_test":     # separate log, judged after >= 30 trades
                ev = self.db.conn.execute("SELECT evidence_json FROM signals WHERE id=?",
                                          (pos["signal_id"],)).fetchone() if pos["signal_id"] else None
                cats = json.loads(ev["evidence_json"]).get("catalysts") if ev and ev["evidence_json"] else None
                self.db.insert("forward_test_trades", {
                    "ts": ts, "position_id": pos["id"], "signal_id": pos["signal_id"], "symbol": pos["symbol"],
                    "asset_class": ac.value, "strategy": pos["strategy"], "opened_ts": pos["opened_ts"],
                    "closed_ts": ts, "entry_price": pos["entry_price"], "exit_price": fill.price, "quantity": qty,
                    "planned_risk_usd": pos["planned_risk_usd"], "fees_usd": fees, "pnl_usd": round(pnl, 8),
                    "r_multiple": None if r_mult is None else round(r_mult, 4), "exit_reason": reason,
                    "catalysts_json": json.dumps(cats, default=str) if cats else None})
        self.db.log_event("paper_exit", "execution", f"{pos['symbol']} {reason} {qty} @ {fill.price:.6g} "
                                                     f"pnl ${pnl:.4f}", "info" if pnl >= 0 else "warning")
        return {"position_id": pos["id"], "reason": reason, "exit_price": fill.price, "pnl": round(pnl, 6),
                "r_multiple": r_mult, "assumption": fill.assumption}

    # ------------------------------------------------------------------ management
    def manage_position(self, pos: dict, bars: list[Candle], now: datetime,
                        quote: MarketQuote | None = None) -> dict:
        """Walk bars that started after the entry (incl. the forming bar's range so far):
        stop first, then target; then time exits; then mark-to-market."""
        ac = AssetClass(pos["asset_class"])
        opened = datetime.fromisoformat(pos["opened_ts"])
        after = datetime.fromisoformat(pos["last_managed_bar"]) if pos["last_managed_bar"] else None
        mfe, mae = pos["mfe_price"] or pos["entry_price"], pos["mae_price"] or pos["entry_price"]
        last_complete = None
        for b in sorted(bars, key=lambda x: x.open_time):
            if b.open_time < opened or (after is not None and b.open_time <= after):
                continue
            mfe, mae = max(mfe, b.high), min(mae, b.low)
            liq = pos.get("liquidation_price")
            if liq and b.low <= pos["stop_loss"] and b.open <= liq:   # gapped through the stop into liquidation
                bk = bankrupt_price(pos["entry_price"], pos["leverage"])
                return self.close_position(pos, Fill(bk, liq, 0.0, f"ISOLATED LIQUIDATION at {liq:.6g}: margin lost"),
                                           "liquidation", now)
            if b.low <= pos["stop_loss"]:
                return self.close_position(pos, self.costs.stop_fill(ac, pos["stop_loss"], b.open), "stop_loss", now)
            if b.high > pos["take_profit"]:
                return self.close_position(pos, self.costs.target_fill(ac, pos["take_profit"], b.open),
                                           "take_profit", now)
            if b.is_complete:
                last_complete = b.open_time
        last = quote.price if quote else (bars[-1].close if bars else None)
        # time exits
        reason = None
        if ac == AssetClass.STOCK and pos.get("strategy") == self.s.swing.strategy:
            pass        # D103: the RSI(2) swing is the ONLY stock strategy allowed to hold overnight (exits in trader)
        elif ac == AssetClass.STOCK:
            close = regular_close_at(now)
            opened_close = regular_close_at(opened)
            if close is None or (opened_close and now >= opened_close - timedelta(
                    minutes=self.s.risk.stock_flatten_minutes_before_close)):
                reason = "eod_flatten"
        elif now - opened >= timedelta(hours=self.s.risk.crypto_max_hold_hours):
            reason = "max_hold_time"
        if reason and last:
            f = self.costs.sell_fill(ac, last, quote.bid if quote else None, quote.ask if quote else None)
            return self.close_position(pos, f, reason, now)
        if last:
            unreal = (last - pos["entry_price"]) * pos["quantity"] - pos["fees_usd"]
            self.db.conn.execute(
                "UPDATE positions SET mark_price=?, mark_ts=?, unrealized_pnl_usd=?, mfe_price=?, mae_price=?, "
                "last_managed_bar=COALESCE(?, last_managed_bar) WHERE id=? AND status='open'",
                (last, now.isoformat(), round(unreal, 8), mfe, mae,
                 last_complete.isoformat() if last_complete else None, pos["id"]))
        return {"position_id": pos["id"], "status": "open", "mark": last}

    def flatten_all(self, marks: dict[str, MarketQuote | None], reason: str, now: datetime) -> list[dict]:
        out = []
        for pos in self.ledger.open_positions():
            ac = AssetClass(pos["asset_class"])
            q = marks.get(pos["symbol"])
            last = q.price if q else pos["mark_price"]
            note = "" if q else " (no live quote: last mark used)"
            f = self.costs.sell_fill(ac, last, q.bid if q else None, q.ask if q else None)
            f = Fill(f.price, f.reference, f.slippage_per_unit, f.assumption + note)
            out.append(self.close_position(pos, f, reason, now))
        return out
