"""Normalized market-data models shared by every provider."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


class AssetClass(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"


class DataStatus(str, Enum):
    LIVE = "live"            # fresh data straight from the source
    DELAYED = "delayed"      # real but older than the live threshold (or source is delayed / market closed)
    CACHED = "cached"        # served from the local database, not fetched now
    UNAVAILABLE = "unavailable"


def tf_seconds(timeframe: str) -> int:
    try:
        return TIMEFRAME_SECONDS[timeframe]
    except KeyError:
        raise ValueError(f"unsupported timeframe {timeframe!r}; use one of {list(TIMEFRAME_SECONDS)}")


def grid_seconds(timeframe: str, asset_class: "AssetClass") -> int:
    """Alignment grid for bar open times. Yahoo's hourly stock bars are anchored
    to the 09:30 ET open (09:00, 14:00, 14:30, 15:30 ... UTC), so a 30-minute
    grid is used for stock 1h bars."""
    if timeframe == "1h" and asset_class == AssetClass.STOCK:
        return 1800
    return tf_seconds(timeframe)


def utc(ts: float | int) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


@dataclass(frozen=True)
class Candle:
    symbol: str                 # canonical ATLAS symbol, e.g. "AAPL" or "BTC-USD"
    asset_class: AssetClass
    source: str
    timeframe: str
    open_time: datetime         # UTC, start of the bar
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_complete: bool = True    # False = bar still forming; never use for signals
    session: str | None = None  # stocks: pre | regular | post ; crypto: None (24/7)

    @property
    def close_time(self) -> datetime:
        if self.timeframe == "1d" and self.asset_class == AssetClass.STOCK:
            # daily stock bar closes at 16:00 ET (13:00 on early-close days)
            from app.data.calendar import EARLY_CLOSE, EARLY_CLOSES, ET, REG_CLOSE
            d = self.open_time.astimezone(ET).date()
            t = EARLY_CLOSE if d in EARLY_CLOSES else REG_CLOSE
            return datetime.combine(d, t, tzinfo=ET).astimezone(timezone.utc)
        return self.open_time + timedelta(seconds=tf_seconds(self.timeframe))


@dataclass(frozen=True)
class Quote:
    symbol: str
    asset_class: AssetClass
    source: str
    price: float
    source_ts: datetime | None
    bid: float | None = None
    ask: float | None = None
    quote_currency: str = "USD"   # Binance quotes are USDT -> documented basis vs USD

    @property
    def spread_bps(self) -> float | None:
        if self.bid and self.ask and self.ask >= self.bid > 0:
            mid = (self.bid + self.ask) / 2
            return (self.ask - self.bid) / mid * 1e4
        return None


@dataclass
class CandleSeries:
    symbol: str
    asset_class: AssetClass
    source: str
    timeframe: str
    candles: list[Candle]
    fetched_at: datetime
    status: DataStatus = DataStatus.UNAVAILABLE
    age_seconds: float | None = None    # now - close_time of the last COMPLETE candle
    stale: bool = False                 # too old to trade on while the market is open
    market_open: bool | None = None
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> list[Candle]:
        return [c for c in self.candles if c.is_complete]

    @property
    def last_complete(self) -> Candle | None:
        cs = self.complete
        return cs[-1] if cs else None

    @property
    def tradable(self) -> bool:
        """Usable for live signal generation: real, fresh, valid, non-empty."""
        return (self.status == DataStatus.LIVE and not self.stale and not self.issues
                and self.last_complete is not None)
