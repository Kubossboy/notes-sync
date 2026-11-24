"""Alpaca PAPER mirror (DECISIONS D85).

The internal $1,000 ledger is the source of truth; every approved internal entry (validated AND
forward_test) is mirrored to https://paper-api.alpaca.markets with the SAME quantity the ATLAS
risk engine sized from its own ledger - never from Alpaca's $100k paper balance / $200k buying power.

* Stocks, whole-share qty -> bracket order (stop + target live at Alpaca).
* Stocks with fractional qty, and crypto -> plain market buy (brackets can't carry these);
  the exit is mirrored when the internal ledger closes the position.
* Skips (journaled as `alpaca_mirror_skipped`): asset not tradable/inactive at Alpaca, fractional qty on a
  non-fractionable stock, notional above the ATLAS ledger equity (no leverage), crypto disabled on the account.
* Any Alpaca error is logged and never affects the internal trade.
"""
from __future__ import annotations

import math

from app.execution.alpaca_paper import AlpacaPaperBroker


def alpaca_symbol(symbol: str, asset_class: str) -> str:
    return symbol.replace("-", "/") if asset_class == "crypto" else symbol


class AlpacaMirror:
    def __init__(self, broker: AlpacaPaperBroker, db):
        self.b, self.db = broker, db
        self._assets: dict[str, object] = {}

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.b, "orders_enabled", False))

    def _skip(self, symbol: str, why: str) -> dict:
        self.db.log_event("alpaca_mirror_skipped", "execution", f"{symbol}: {why}", "info")
        return {"mirrored": False, "reason": why}

    def _asset(self, symbol: str):
        if symbol not in self._assets:
            self._assets[symbol] = self.b.asset(symbol)
        return self._assets[symbol]

    def open(self, symbol: str, asset_class: str, qty: float, entry: float, stop: float, target: float,
             client_order_id: str, order_id: int, ledger_equity: float) -> dict:
        if not self.enabled:
            return {"mirrored": False, "reason": "disabled"}
        try:
            if qty <= 0 or qty * entry > ledger_equity:
                return self._skip(symbol, f"notional ${qty * entry:.2f} exceeds ATLAS ledger equity (no leverage)")
            a = self._asset(symbol)
            if not a.tradable or a.status != "active":
                return self._skip(symbol, f"not tradable at Alpaca (status {a.status})")
            asym = alpaca_symbol(symbol, asset_class)
            whole = math.isclose(qty, round(qty), abs_tol=1e-9) and round(qty) >= 1
            if asset_class == "stock" and whole:
                o = self.b.submit_bracket(asym, int(round(qty)), stop, target, client_order_id)
                kind = "bracket"
            else:
                if not a.fractionable:
                    return self._skip(symbol, f"fractional qty {qty:g} but asset not fractionable")
                o = self.b.submit_market(asym, qty, client_order_id, crypto=asset_class == "crypto")
                kind = "market"
            self.db.conn.execute("UPDATE order_requests SET external_order_id=?, external_status=? WHERE id=?",
                                 (o.get("id"), f"alpaca_paper:{kind}:{o.get('status')}", order_id))
            self.db.log_event("alpaca_mirror_opened", "execution", f"{asym} {kind} qty {qty:g}", "info")
            return {"mirrored": True, "kind": kind, "status": o.get("status")}
        except Exception as e:
            self.db.log_error("execution.alpaca_paper", e)
            self.db.log_event("alpaca_mirror_failed", "execution", str(e)[:300], "warning")
            return {"mirrored": False, "reason": f"error: {type(e).__name__}"}

    def close(self, position_id: int, symbol: str, asset_class: str) -> dict:
        """Mirror an internal exit; only for positions whose entry was actually mirrored."""
        if not self.enabled:
            return {"mirrored": False, "reason": "disabled"}
        row = self.db.conn.execute(
            "SELECT external_order_id FROM order_requests WHERE position_id=? AND purpose='entry' "
            "AND external_order_id IS NOT NULL", (position_id,)).fetchone()
        if not row:
            return {"mirrored": False, "reason": "entry not mirrored"}
        try:
            r = self.b.close_position(alpaca_symbol(symbol, asset_class))
            self.db.log_event("alpaca_mirror_closed", "execution", f"{symbol}: {r.get('status', 'submitted')}", "info")
            return {"mirrored": True, "status": r.get("status")}
        except Exception as e:
            self.db.log_error("execution.alpaca_paper", e)
            self.db.log_event("alpaca_mirror_failed", "execution", f"close {symbol}: {str(e)[:250]}", "warning")
            return {"mirrored": False, "reason": f"error: {type(e).__name__}"}
