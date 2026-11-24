"""Cross-source price verification (median consensus)."""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from app.config import DataSection
from app.data.models import AssetClass


@dataclass
class CrossCheck:
    symbol: str
    asset_class: AssetClass
    kind: str
    prices: dict[str, float]
    consensus: float | None = None
    deviations_bps: dict[str, float] = field(default_factory=dict)
    outliers: list[str] = field(default_factory=list)
    verdict: str = "unavailable"   # agree | single_source | outlier_excluded | divergent | unavailable

    @property
    def max_deviation_bps(self) -> float | None:
        return max(self.deviations_bps.values()) if self.deviations_bps else None

    @property
    def reliable(self) -> bool:
        return self.verdict in ("agree", "outlier_excluded")


def cross_check(symbol: str, asset_class: AssetClass, prices: dict[str, float], cfg: DataSection,
                kind: str = "quote") -> CrossCheck:
    prices = {k: v for k, v in prices.items() if v is not None and v > 0}
    cc = CrossCheck(symbol, asset_class, kind, prices)
    if not prices:
        return cc
    if len(prices) == 1:
        cc.consensus = next(iter(prices.values()))
        cc.deviations_bps = {k: 0.0 for k in prices}
        cc.verdict = "single_source"
        return cc
    tol = cfg.crosscheck_tolerance_bps_crypto if asset_class == AssetClass.CRYPTO else cfg.crosscheck_tolerance_bps_stock
    med = median(prices.values())
    cc.deviations_bps = {k: round(abs(v / med - 1) * 1e4, 2) for k, v in prices.items()}
    cc.outliers = sorted(k for k, d in cc.deviations_bps.items() if d > tol)
    agreeing = {k: v for k, v in prices.items() if k not in cc.outliers}
    need = cfg.min_sources_crypto if asset_class == AssetClass.CRYPTO else 1
    if len(agreeing) >= max(need, 1) and len(agreeing) > len(cc.outliers):
        cc.consensus = median(agreeing.values())
        cc.verdict = "outlier_excluded" if cc.outliers else "agree"
    else:
        cc.consensus = None
        cc.verdict = "divergent"
    return cc
