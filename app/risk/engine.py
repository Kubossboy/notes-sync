"""DETERMINISTIC RISK ENGINE (spec section 6).

Pure rules on numbers - no language model, no discretion. Every evaluation writes
one `risk_evaluations` row plus one `risk_checks` row per rule. Any exception
inside the engine = risk engine failure -> SAFE STATE + rejection (fail closed).
The AI/strategy layer can only *propose*; it cannot override a rejection.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.core.logging_setup import get_logger
from app.core.safety import SafeState
from app.data.calendar import regular_close_at, session_at
from app.data.models import AssetClass
from app.database.db import Database
from app.execution.costs import CostModel
from app.portfolio.ledger import Ledger, PortfolioState

log = get_logger("risk")


@dataclass
class Proposal:
    signal_id: int | None
    symbol: str
    asset_class: AssetClass
    strategy: str
    direction: str
    entry: float | None
    stop: float | None
    target: float | None
    score: float | None
    status: str
    expires_ts: datetime | None
    data_status: str | None

    @classmethod
    def from_signal_row(cls, r) -> "Proposal":
        return cls(r["id"], r["symbol"], AssetClass(r["asset_class"]), r["strategy"], r["direction"],
                   r["entry_price"], r["stop_loss"], r["take_profit"], r["total_score"], r["status"],
                   datetime.fromisoformat(r["expires_ts"]) if r["expires_ts"] else None, r["data_status"])


@dataclass
class MarketQuote:
    price: float
    bid: float | None
    ask: float | None
    ts: datetime | None
    verdict: str                      # cross-check verdict
    avg_bar_dollar_volume: float | None = None


@dataclass
class Check:
    name: str
    passed: bool
    value: float | None = None
    limit: float | None = None
    detail: str = ""


@dataclass
class RiskDecision:
    evaluation_id: str
    approved: bool
    quantity: float = 0.0
    notional: float = 0.0
    planned_risk_usd: float = 0.0
    risk_budget_usd: float = 0.0
    expected_fill: float | None = None
    est_fees_usd: float = 0.0
    rr_net: float | None = None
    checks: list[Check] = field(default_factory=list)
    reason: str | None = None

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


class RiskEngine:
    def __init__(self, settings: Settings, db: Database, safe: SafeState, ledger: Ledger | None = None):
        self.s, self.db, self.safe = settings, db, safe
        self.r, self.ex = settings.risk, settings.execution
        self.ledger = ledger or Ledger(settings, db)
        self.costs = CostModel(settings)

    # ------------------------------------------------------------------ public
    def evaluate(self, p: Proposal, q: MarketQuote | None, now: datetime | None = None,
                 marks: dict[str, float] | None = None) -> RiskDecision:
        now = now or datetime.now(timezone.utc)
        eid = uuid.uuid4().hex[:16]
        try:
            st = self.ledger.state(now, marks)
            d = self._evaluate(eid, p, q, now, st)
        except Exception as e:                     # risk engine failure -> fail closed
            self.db.log_error("risk", e)
            self.safe.halt(f"risk engine failure: {type(e).__name__}: {e}", module="risk")
            d = RiskDecision(eid, False, checks=[Check("risk_engine_ok", False, detail=str(e))],
                             reason=f"risk engine failure: {e}")
            st = None
        self._journal(d, p, now, st)
        return d

    # ------------------------------------------------------------------ rules
    def _evaluate(self, eid: str, p: Proposal, q: MarketQuote | None, now: datetime,
                  st: PortfolioState) -> RiskDecision:
        C: list[Check] = []
        add = lambda name, ok, value=None, limit=None, detail="": C.append(Check(name, bool(ok), value, limit, detail))  # noqa: E731
        ac = p.asset_class

        add("safe_state_normal", self.safe.trading_allowed(), detail=self.safe.status().reason or "")
        add("execution_mode_paper", self.ex.mode == "paper", detail=self.ex.mode)
        add("long_only", p.direction == "long" or self.ex.allow_short, detail=p.direction)
        add("signal_complete", p.status == "new",
            detail=f"signal status '{p.status}' (only complete, non-rejected signals are executable)")
        add("score_threshold", (p.score or 0) >= self.s.scoring.min_score_to_trade, p.score,
            self.s.scoring.min_score_to_trade)
        add("signal_not_expired", p.expires_ts is None or now < p.expires_ts,
            detail=f"expires {p.expires_ts.isoformat() if p.expires_ts else '-'}")
        add("data_live", p.data_status == "live", detail=f"signal data {p.data_status}")

        # stop / target validity (spec: detect missing or invalid stop-loss)
        valid_levels = all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0
                           for x in (p.entry, p.stop, p.target))
        add("stop_loss_present_valid", valid_levels and p.stop < p.entry,
            p.stop, p.entry, "stop must exist, be > 0 and below entry (long)")
        add("take_profit_present_valid", valid_levels and p.target > p.entry, p.target, p.entry,
            "target must exist and be above entry (long)")

        # market quote quality ("if data is unreliable, trading must stop")
        q_ok = q is not None and q.price > 0 and q.verdict in ("agree", "single_source", "outlier_excluded")
        add("quote_reliable", q_ok, detail=f"verdict {q.verdict if q else 'none'}")
        if q is not None and q.ts is not None:
            age = (now - q.ts).total_seconds()
            add("quote_fresh", age <= self.ex.max_quote_age_seconds, round(age, 1), self.ex.max_quote_age_seconds)

        # session rules (stocks: regular session only, no entries just before the close)
        if ac == AssetClass.STOCK:
            sess = session_at(now)
            add("stock_regular_session", sess == "regular", detail=sess)
            close = regular_close_at(now)
            if close is not None and sess == "regular":
                mins = (close - now).total_seconds() / 60
                add("stock_entry_window", mins > self.r.stock_no_new_entries_minutes_before_close, round(mins, 1),
                    self.r.stock_no_new_entries_minutes_before_close, "minutes to close")

        # portfolio rules
        add("max_open_positions", len(st.open_positions) < self.r.max_open_positions,
            len(st.open_positions), self.r.max_open_positions)
        add("no_existing_position_in_symbol", all(x["symbol"] != p.symbol for x in st.open_positions),
            detail="no pyramiding / averaging down")
        dup = self.db.conn.execute("SELECT id FROM order_requests WHERE signal_id=? AND purpose='entry'",
                                   (p.signal_id,)).fetchone() if p.signal_id is not None else None
        add("no_duplicate_order_for_signal", dup is None, detail=f"existing order {dup[0]}" if dup else "")
        daily_limit = st.day_start_equity * self.r.max_daily_loss_pct / 100
        add("daily_loss_limit_not_reached", st.daily_loss < daily_limit, round(st.daily_loss, 4),
            round(daily_limit, 4), f"day {st.trading_day}")
        add("consecutive_losses_today", st.consecutive_losses_today < self.r.max_consecutive_losses_per_day,
            st.consecutive_losses_today, self.r.max_consecutive_losses_per_day, "anti revenge trading")
        last_loss = self.ledger.last_loss_ts(p.symbol)
        if last_loss is not None and self.r.symbol_cooldown_minutes_after_loss > 0:
            since = (now - last_loss).total_seconds() / 60
            add("symbol_cooldown_after_loss", since >= self.r.symbol_cooldown_minutes_after_loss,
                round(since, 1), self.r.symbol_cooldown_minutes_after_loss, "minutes since last loss in symbol")

        d = RiskDecision(eid, False, checks=C)
        # ---- sizing (only meaningful with valid levels and a quote)
        if valid_levels and q_ok and p.stop < p.entry < p.target:
            fill = self.costs.buy_fill(ac, q.price, q.bid, q.ask)
            add("price_not_below_stop", fill.price > p.stop, fill.price, p.stop)
            r_unit = p.entry - p.stop
            drift = (fill.price - p.entry) / r_unit
            add("no_chasing_entry_drift", drift <= self.r.max_entry_drift_r, round(drift, 3),
                self.r.max_entry_drift_r, "R units between signal entry and expected fill")
            fee_b = self.costs.fee_bps(ac) / 1e4
            stop_exit = p.stop * (1 - self.costs.slip(ac))           # expected stop fill
            per_unit_loss = (fill.price - stop_exit) + fill.price * fee_b + stop_exit * fee_b
            per_unit_gain = (p.target - fill.price) - fill.price * fee_b - p.target * fee_b
            budget = st.equity * self.r.max_risk_per_trade_pct / 100
            remaining_day = max(0.0, daily_limit - st.daily_loss)
            comm = 2 * self.ex.commission_per_trade_usd
            eff_budget = max(0.0, min(budget, remaining_day) - comm)
            qty_risk = eff_budget / per_unit_loss if per_unit_loss > 0 else 0.0
            qty_pos = st.equity * self.r.max_position_pct_equity / 100 / fill.price
            qty_cash = max(0.0, st.cash - comm) / (fill.price * (1 + fee_b))      # no leverage / margin
            qty = self.costs.round_qty(min(qty_risk, qty_pos, qty_cash), ac)
            notional = qty * fill.price
            planned = qty * per_unit_loss + comm
            fees = self.costs.fee(notional, ac) + self.costs.fee(qty * p.target, ac)
            rr = (qty * per_unit_gain - comm) / planned if planned > 0 else None
            add("position_size_positive", qty > 0, qty, None, "after rounding to lot step")
            add("min_notional", notional >= self.ex.min_notional_usd, round(notional, 4), self.ex.min_notional_usd)
            add("risk_within_limit", planned <= budget + 1e-9, round(planned, 4), round(budget, 4),
                f"{self.r.max_risk_per_trade_pct}% of current equity {st.equity:.2f}")
            add("risk_within_remaining_daily_budget", planned <= remaining_day + 1e-9, round(planned, 4),
                round(remaining_day, 4))
            add("no_leverage_cash_covers_notional", notional * (1 + fee_b) + comm <= st.cash + 1e-9,
                round(notional, 4), round(st.cash, 4))
            add("net_reward_risk", rr is not None and rr >= self.r.min_reward_risk,
                None if rr is None else round(rr, 3), self.r.min_reward_risk, "after fees, spread and slippage")
            if q.avg_bar_dollar_volume:
                pct = notional / q.avg_bar_dollar_volume * 100
                add("order_vs_liquidity", pct <= self.ex.max_order_pct_bar_dollar_volume, round(pct, 4),
                    self.ex.max_order_pct_bar_dollar_volume, "% of average bar dollar volume")
            d = RiskDecision(eid, False, qty, round(notional, 6), round(planned, 6), round(budget, 6),
                             fill.price, round(fees, 6), None if rr is None else round(rr, 3), C)
        else:
            add("sizing_possible", False, detail="invalid levels or no reliable quote")
        d.checks = C
        d.approved = all(c.passed for c in C)
        d.reason = None if d.approved else "; ".join(f"{c.name}" + (f" ({c.detail})" if c.detail else "")
                                                       for c in C if not c.passed)
        return d

    # ------------------------------------------------------------------ journal
    def _journal(self, d: RiskDecision, p: Proposal, now: datetime, st: PortfolioState | None) -> None:
        ts = now.isoformat()
        with self.db.transaction():
            self.db.insert("risk_evaluations", {
                "evaluation_id": d.evaluation_id, "ts": ts, "signal_id": p.signal_id, "symbol": p.symbol,
                "approved": int(d.approved), "quantity": d.quantity, "notional_usd": d.notional,
                "planned_risk_usd": d.planned_risk_usd, "risk_budget_usd": d.risk_budget_usd,
                "expected_fill": d.expected_fill, "est_fees_usd": d.est_fees_usd, "rr_net": d.rr_net,
                "equity_usd": st.equity if st else None, "cash_usd": st.cash if st else None,
                "day_start_equity_usd": st.day_start_equity if st else None,
                "daily_loss_usd": st.daily_loss if st else None, "reason": d.reason,
                "details_json": json.dumps({"proposal": {k: (v.isoformat() if isinstance(v, datetime) else
                                                             getattr(v, "value", v)) for k, v in asdict(p).items()}})})
            for c in d.checks:
                self.db.insert("risk_checks", {
                    "ts": ts, "signal_id": p.signal_id, "check_name": c.name, "passed": int(c.passed),
                    "value": c.value, "limit_value": c.limit, "detail": c.detail or None,
                    "evaluation_id": d.evaluation_id})
