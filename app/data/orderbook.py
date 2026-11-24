"""Public order-book snapshots (crypto) and depth/imbalance metrics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.data.models import AssetClass
from app.data.providers.base import BaseProvider, DataFormatError, SymbolNotSupported, fnum
from app.data.providers.crypto import BinanceProvider, CoinbaseProvider, KrakenProvider


@dataclass(frozen=True)
class BookSnapshot:
    symbol: str
    source: str
    ts: datetime
    bids: list[tuple[float, float]]   # (price, size), best first
    asks: list[tuple[float, float]]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.best_ask - self.best_bid) / self.mid * 1e4

    def depth_usd(self, pct: float) -> tuple[float, float]:
        """USD notional resting within pct% of mid on (bid side, ask side)."""
        lo, hi = self.mid * (1 - pct / 100), self.mid * (1 + pct / 100)
        b = sum(p * s for p, s in self.bids if p >= lo)
        a = sum(p * s for p, s in self.asks if p <= hi)
        return b, a

    def imbalance(self, pct: float) -> float:
        b, a = self.depth_usd(pct)
        return (b - a) / (b + a) if (b + a) > 0 else 0.0


def validate_book(b: BookSnapshot) -> None:
    if not b.bids or not b.asks:
        raise DataFormatError(f"{b.source}: empty order book")
    if b.best_ask <= b.best_bid:
        raise DataFormatError(f"{b.source}: crossed book bid={b.best_bid} ask={b.best_ask}")
    if any(b.bids[i][0] < b.bids[i + 1][0] for i in range(len(b.bids) - 1)) or \
       any(b.asks[i][0] > b.asks[i + 1][0] for i in range(len(b.asks) - 1)):
        raise DataFormatError(f"{b.source}: book levels not sorted")
    if any(s < 0 for _, s in b.bids + b.asks):
        raise DataFormatError(f"{b.source}: negative size")


def _levels(rows, src, n=None):
    out = []
    for r in rows[:n] if n else rows:
        out.append((fnum(r[0], "price", src), fnum(r[1], "size", src)))
    return out


def fetch_book(provider: BaseProvider, symbol: str, depth: int = 100) -> BookSnapshot:
    now = datetime.now(timezone.utc)
    if isinstance(provider, CoinbaseProvider):
        j = provider._get_json(f"{provider.BASE}/products/{provider.map_symbol(symbol)}/book", {"level": 2})
        b = BookSnapshot(symbol, provider.name, now, _levels(j["bids"], "coinbase", depth),
                         _levels(j["asks"], "coinbase", depth))
    elif isinstance(provider, KrakenProvider):
        j = provider._result(provider._get_json(f"{provider.BASE}/Depth",
                                                {"pair": provider.map_symbol(symbol), "count": min(depth, 500)}))
        b = BookSnapshot(symbol, provider.name, now, _levels(j["bids"], "kraken"), _levels(j["asks"], "kraken"))
    elif isinstance(provider, BinanceProvider):
        j = provider._get_json(f"{provider.BASE}/depth", {"symbol": provider.map_symbol(symbol),
                                                         "limit": min(depth, 5000)})
        b = BookSnapshot(symbol, provider.name, now, _levels(j["bids"], "binance"), _levels(j["asks"], "binance"))
    else:
        raise SymbolNotSupported(f"{provider.name}: no order book support")
    validate_book(b)
    return b
