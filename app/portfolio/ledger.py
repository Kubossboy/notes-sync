"""ATLAS's own paper ledger ($1,000 start). The internal ledger is the single source
of truth for equity, cash, P&L and daily loss - regardless of any external paper broker.

cash   = start + Σ realized P&L (closed, net of all fees) − Σ open cost (qty × entry + entry fees)
equity = cash + Σ open qty × mark
Trading day = America/New_York date. Day-start equity is frozen the first time a day is seen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import Settings
from app.data.calendar import trading_day_of
from app.database.db import Database


@dataclass
class PortfolioState:
    ts: datetime
    trading_day: str
    cash: float
    positions_value: float
    equity: float
    realized_total: float
    unrealized: float
    day_start_equity: float
    day_realized: float
    open_positions: list[dict] = field(default_factory=list)
    consecutive_losses_today: int = 0

    @property
    def daily_pnl(self) -> float:
        return self.equity - self.day_start_equity

    @property
    def daily_loss(self) -> float:
        return max(0.0, -self.daily_pnl)


class Ledger:
    def __init__(self, settings: Settings, db: Database):
        self.s, self.db = settings, db
        self.start = settings.portfolio.starting_equity_usd

    def open_positions(self) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY id")]

    def state(self, now: datetime | None = None, marks: dict[str, float] | None = None) -> PortfolioState:
        now = now or datetime.now(timezone.utc)
        marks = marks or {}
        day = trading_day_of(now).isoformat()
        realized = self.db.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd),0) FROM positions WHERE status='closed'").fetchone()[0]
        opens = self.open_positions()
        open_cost = sum(p["quantity"] * p["entry_price"] + p["fees_usd"] for p in opens)
        cash = self.start + realized - open_cost
        pv = unreal = 0.0
        for p in opens:
            m = marks.get(p["symbol"]) or p["mark_price"] or p["entry_price"]
            pv += p["quantity"] * m
            unreal += (m - p["entry_price"]) * p["quantity"] - p["fees_usd"]
        equity = cash + pv
        day_realized = self.db.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd),0) FROM positions WHERE status='closed' AND "
            "trading_day_closed=?", (day,)).fetchone()[0]
        key = f"day_start_equity:{day}"
        dse = self.db.get_state(key)
        if dse is None:
            dse = round(equity, 6)
            self.db.set_state(key, dse)
            self.db.insert("equity_snapshots", {
                "ts": now.isoformat(), "cash_usd": cash, "positions_value_usd": pv, "equity_usd": equity,
                "realized_pnl_usd": realized, "unrealized_pnl_usd": unreal, "day_start_equity_usd": dse,
                "note": f"day start {day}"})
        return PortfolioState(now, day, round(cash, 8), round(pv, 8), round(equity, 8), round(realized, 8),
                              round(unreal, 8), float(dse), round(day_realized, 8), opens,
                              self.consecutive_losses(day))

    def consecutive_losses(self, day: str) -> int:
        rows = self.db.conn.execute(
            "SELECT realized_pnl_usd FROM positions WHERE status='closed' AND trading_day_closed=? "
            "ORDER BY closed_ts DESC, id DESC", (day,)).fetchall()
        n = 0
        for r in rows:
            if (r[0] or 0) < 0:
                n += 1
            else:
                break
        return n

    def last_loss_ts(self, symbol: str) -> datetime | None:
        r = self.db.conn.execute(
            "SELECT closed_ts FROM positions WHERE symbol=? AND status='closed' AND realized_pnl_usd < 0 "
            "ORDER BY closed_ts DESC LIMIT 1", (symbol,)).fetchone()
        return datetime.fromisoformat(r[0]) if r else None

    def snapshot(self, st: PortfolioState, note: str = "") -> int:
        return self.db.insert("equity_snapshots", {
            "ts": st.ts.isoformat(), "cash_usd": st.cash, "positions_value_usd": st.positions_value,
            "equity_usd": st.equity, "realized_pnl_usd": st.realized_total, "unrealized_pnl_usd": st.unrealized,
            "day_start_equity_usd": st.day_start_equity, "note": note or None})
