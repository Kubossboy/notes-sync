"""PAPER TRADER: one deterministic cycle =
manage open positions -> (if trading allowed) scan -> risk-evaluate complete signals
-> simulate fills -> journal -> equity snapshot. Kill switch included."""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.core.logging_setup import get_logger
from app.core.safety import SafeState
from app.data.engine import MarketDataEngine, asset_class_of
from app.data.models import AssetClass, tf_seconds
from app.database.db import Database
from app.execution.alpaca_paper import AlpacaPaperBroker
from app.execution.simulator import DuplicateOrder, PaperSimulator
from app.portfolio.ledger import Ledger
from app.risk.engine import MarketQuote, Proposal, RiskEngine

log = get_logger("trader")


class PaperTrader:
    def __init__(self, settings: Settings, db: Database, safe: SafeState, engine: MarketDataEngine,
                 scanner=None, alpaca: AlpacaPaperBroker | None = None):
        self.s, self.db, self.safe, self.engine, self.scanner = settings, db, safe, engine, scanner
        self.ledger = Ledger(settings, db)
        self.risk = RiskEngine(settings, db, safe, self.ledger)
        self.sim = PaperSimulator(settings, db, safe, self.ledger)
        self.alpaca = alpaca

    # ------------------------------------------------------------------ market access
    def quote(self, symbol: str, now: datetime) -> MarketQuote | None:
        cc, quotes, _ = self.engine.get_quote_consensus(symbol)
        if cc.consensus is None:
            return None
        maxsp = self.s.data.max_quote_spread_bps
        book = [q for q in quotes if q.bid and q.ask and q.quote_currency == "USD"
                and q.spread_bps is not None and q.spread_bps <= maxsp]
        best = book[0] if book else None
        ts = max((q.source_ts for q in quotes if q.source_ts), default=None) or now
        adv = None
        try:
            tf = (self.s.scanner.stock_timeframe if asset_class_of(symbol) == AssetClass.STOCK
                  else self.s.scanner.crypto_timeframe)
            c = self.engine.get_candles(symbol, tf, 30, crosscheck=False).series.complete[-20:]
            if c:
                adv = sum(k.close * k.volume for k in c) / len(c)
        except Exception:
            pass
        price = best.price if best else cc.consensus
        return MarketQuote(price, best.bid if best else None, best.ask if best else None, ts, cc.verdict, adv)

    # ------------------------------------------------------------------ cycle parts
    def manage(self, now: datetime) -> list[dict]:
        out = []
        for pos in self.ledger.open_positions():
            sym = pos["symbol"]
            try:
                opened = datetime.fromisoformat(pos["opened_ts"])
                mins = int((now - opened).total_seconds() / tf_seconds(self.s.execution.manage_timeframe)) + 5
                fr = self.engine.get_candles(sym, self.s.execution.manage_timeframe, min(1000, max(10, mins)),
                                             crosscheck=False, now=now)
                bars = fr.series.candles
                if not bars:
                    raise RuntimeError("no bars")
                try:
                    q = self.quote(sym, now)
                except Exception:
                    q = None
                out.append({"symbol": sym, **self.sim.manage_position(pos, bars, now, q)})
            except Exception as e:
                # cannot watch the stop -> fail closed for NEW trades; position stays open, retried next cycle
                self.db.log_error("execution.manage", e)
                self.safe.halt(f"cannot manage open position {sym}: {type(e).__name__}: {e}", module="execution")
                out.append({"symbol": sym, "error": str(e)})
        return out

    def candidates(self, run_ids: list[int]) -> list:
        if not run_ids:
            return []
        qm = ",".join("?" * len(run_ids))
        return self.db.conn.execute(
            f"SELECT s.*, sc.total_score FROM signals s JOIN signal_scores sc ON sc.signal_id = s.id "
            f"WHERE s.scan_run_id IN ({qm}) AND s.status = 'new' ORDER BY sc.total_score DESC", run_ids).fetchall()

    def execute_signal(self, row, now: datetime) -> dict:
        p = Proposal.from_signal_row(row)
        try:
            q = self.quote(p.symbol, now)
        except Exception as e:
            self.db.log_error("execution.quote", e)
            q = None
        d = self.risk.evaluate(p, q, now)
        res = {"signal_id": p.signal_id, "symbol": p.symbol, "strategy": p.strategy, "approved": d.approved,
               "evaluation_id": d.evaluation_id, "reason": d.reason, "qty": d.quantity,
               "planned_risk_usd": d.planned_risk_usd, "rr_net": d.rr_net}
        if not d.approved:
            self.db.insert("strategy_decisions", {"ts": now.isoformat(), "signal_id": p.signal_id,
                                                  "decision": "reject", "reason": f"risk: {d.reason}"})
            return res
        try:
            res["fill"] = self.sim.open_position(p, d, q, now)
        except DuplicateOrder as e:
            res.update(approved=False, reason=str(e))
            return res
        if self.alpaca is not None and self.alpaca.orders_enabled and p.asset_class == AssetClass.STOCK:
            try:   # mirror only; the internal ledger stays authoritative
                o = self.alpaca.submit_bracket(p.symbol, d.quantity, p.stop, p.target, res["fill"]["client_order_id"])
                self.db.conn.execute("UPDATE order_requests SET external_order_id=?, external_status=? WHERE id=?",
                                     (o.get("id"), o.get("status"), res["fill"]["order_id"]))
            except Exception as e:
                self.db.log_error("execution.alpaca_paper", e)
                self.db.log_event("alpaca_mirror_failed", "execution", str(e)[:300], "warning")
        return res

    # ------------------------------------------------------------------ public
    def run_once(self, now: datetime | None = None, scan: bool = True) -> dict:
        now = now or datetime.now(timezone.utc)
        report = {"ts": now.isoformat(), "managed": self.manage(now), "executions": [], "scans": []}
        if scan and self.scanner is not None and self.safe.trading_allowed():
            run_ids = []
            for ac in (AssetClass.STOCK, AssetClass.CRYPTO):
                rep = self.scanner.run(ac, mode="live")
                run_ids.append(rep["run_id"])
                report["scans"].append({"scanner": ac.value, "run_id": rep["run_id"], "signals": rep["signals"]})
            for row in self.candidates(run_ids):
                if not self.safe.trading_allowed():
                    break
                report["executions"].append(self.execute_signal(row, now))
        st = self.ledger.state(now)
        self.ledger.snapshot(st, "cycle")
        report["portfolio"] = summary(st, self.s)
        report["safe_state"] = self.safe.status().reason
        return report

    def kill(self, reason: str, flatten: bool, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        self.safe.halt(f"KILL SWITCH: {reason}", module="kill_switch")
        closed = []
        if flatten:
            marks = {}
            for pos in self.ledger.open_positions():
                try:
                    marks[pos["symbol"]] = self.quote(pos["symbol"], now)
                except Exception:
                    marks[pos["symbol"]] = None
            closed = self.sim.flatten_all(marks, "kill_switch", now)
        st = self.ledger.state(now)
        self.ledger.snapshot(st, "kill switch")
        return {"halted": True, "closed": closed, "portfolio": summary(st, self.s)}


def summary(st, settings: Settings) -> dict:
    limit = st.day_start_equity * settings.risk.max_daily_loss_pct / 100
    return {"equity": round(st.equity, 4), "cash": round(st.cash, 4), "positions_value": round(st.positions_value, 4),
            "realized_total": round(st.realized_total, 4), "unrealized": round(st.unrealized, 4),
            "total_pnl": round(st.equity - settings.portfolio.starting_equity_usd, 4),
            "trading_day": st.trading_day, "day_start_equity": st.day_start_equity,
            "daily_pnl": round(st.daily_pnl, 4), "daily_loss_limit": round(limit, 4),
            "daily_limit_reached": st.daily_loss >= limit, "open_positions": len(st.open_positions),
            "consecutive_losses_today": st.consecutive_losses_today,
            "risk_per_trade_budget": round(st.equity * settings.risk.max_risk_per_trade_pct / 100, 4)}
