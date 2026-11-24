"""Deterministic fill / cost model shared by the risk engine and the paper simulator.

Assumptions (also written into every simulated fill):
* market buy  fills at ask (or last × (1 + assumed half spread)) × (1 + slippage)
* market sell fills at bid (or last × (1 − assumed half spread)) × (1 − slippage)
* stop-loss (stop-market) triggers when a bar trades at/below the stop; fills at
  min(stop, bar open) × (1 − slippage)  -> gaps through the stop are filled at the open
* take-profit (limit) fills only when a bar trades ABOVE the target, at max(target, bar open), no slippage
* stop and target touched in the same bar -> the stop is assumed first (conservative)
* fees = notional × fee bps (+ fixed commission per order)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.config import Settings
from app.data.models import AssetClass


@dataclass(frozen=True)
class Fill:
    price: float
    reference: float
    slippage_per_unit: float
    assumption: str


class CostModel:
    def __init__(self, settings: Settings):
        self.ex = settings.execution

    def fee_bps(self, ac: AssetClass) -> float:
        return self.ex.crypto_fee_bps if ac == AssetClass.CRYPTO else self.ex.stock_fee_bps

    def slip(self, ac: AssetClass) -> float:
        return (self.ex.slippage_bps_crypto if ac == AssetClass.CRYPTO else self.ex.slippage_bps_stock) / 1e4

    def half_spread(self, ac: AssetClass) -> float:
        return (self.ex.assumed_half_spread_bps_crypto if ac == AssetClass.CRYPTO
                else self.ex.assumed_half_spread_bps_stock) / 1e4

    def fee(self, notional: float, ac: AssetClass) -> float:
        return round(notional * self.fee_bps(ac) / 1e4 + self.ex.commission_per_trade_usd, 8)

    def buy_fill(self, ac: AssetClass, last: float, bid: float | None = None, ask: float | None = None) -> Fill:
        if ask and bid and ask >= bid > 0:
            ref, how = ask, "ask"
        else:
            ref, how = last * (1 + self.half_spread(ac)), f"last+{self.half_spread(ac) * 1e4:.1f}bps assumed half-spread"
        px = ref * (1 + self.slip(ac))
        return Fill(px, ref, px - ref, f"market buy at {how} + {self.slip(ac) * 1e4:.1f}bps slippage")

    def sell_fill(self, ac: AssetClass, last: float, bid: float | None = None, ask: float | None = None) -> Fill:
        if ask and bid and ask >= bid > 0:
            ref, how = bid, "bid"
        else:
            ref, how = last * (1 - self.half_spread(ac)), f"last-{self.half_spread(ac) * 1e4:.1f}bps assumed half-spread"
        px = ref * (1 - self.slip(ac))
        return Fill(px, ref, ref - px, f"market sell at {how} - {self.slip(ac) * 1e4:.1f}bps slippage")

    def stop_fill(self, ac: AssetClass, stop: float, bar_open: float) -> Fill:
        ref = min(stop, bar_open)
        px = ref * (1 - self.slip(ac))
        gap = " (gapped through stop: filled from bar open)" if bar_open < stop else ""
        return Fill(px, stop, stop - px, f"stop-market at {ref:.6g} - {self.slip(ac) * 1e4:.1f}bps slippage{gap}")

    def target_fill(self, ac: AssetClass, target: float, bar_open: float) -> Fill:
        px = max(target, bar_open)
        return Fill(px, target, 0.0, "take-profit limit (bar traded above target), no slippage")

    def round_qty(self, qty: float, ac: AssetClass) -> float:
        if ac == AssetClass.STOCK and not self.ex.fractional_stocks:
            return float(math.floor(qty + 1e-12))
        step = self.ex.crypto_qty_step if ac == AssetClass.CRYPTO else self.ex.stock_qty_step
        return round(math.floor(qty / step + 1e-9) * step, 10)
