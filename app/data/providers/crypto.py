"""Public (keyless) crypto market-data providers: Coinbase Exchange, Kraken, Binance.

Canonical ATLAS crypto symbols are "BASE-USD" (e.g. BTC-USD). Binance has no
USD spot book on its global API, so BTC-USD maps to BTCUSDT and the quote is
tagged quote_currency="USDT" (the USDT/USD basis is normally a few bps; the
cross-check tolerance accounts for it).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.data.models import AssetClass, Candle, Quote, tf_seconds, utc
from app.data.providers.base import BaseProvider, DataFormatError, SymbolNotSupported, fnum


def _split(symbol: str) -> tuple[str, str]:
    if "-" not in symbol:
        raise SymbolNotSupported(f"crypto symbol must look like BASE-USD, got {symbol!r}")
    base, quote = symbol.upper().split("-", 1)
    if quote != "USD":
        raise SymbolNotSupported(f"only USD-quoted crypto symbols supported, got {symbol!r}")
    return base, quote


def parse_iso_utc(v) -> datetime | None:
    """Parse ISO timestamps incl. nanosecond precision ('...55.998464629Z')."""
    if not isinstance(v, str):
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$", v.strip())
    if not m:
        return None
    frac = (m.group(2) or ".0")[:7]
    tz = m.group(3) or "Z"
    try:
        return datetime.fromisoformat(m.group(1) + frac + ("+00:00" if tz == "Z" else tz)).astimezone(timezone.utc)
    except ValueError:
        return None


def _now(now):
    return now or datetime.now(timezone.utc)


class CoinbaseProvider(BaseProvider):
    name = "coinbase"
    asset_class = AssetClass.CRYPTO
    BASE = "https://api.exchange.coinbase.com"
    GRAN = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}

    def map_symbol(self, symbol):
        b, q = _split(symbol)
        return f"{b}-{q}"

    def fetch_candles(self, symbol, timeframe, limit=100, now=None):
        if timeframe not in self.GRAN:
            raise SymbolNotSupported(f"coinbase: timeframe {timeframe}")
        rows = self._get_json(f"{self.BASE}/products/{self.map_symbol(symbol)}/candles",
                              {"granularity": self.GRAN[timeframe]})
        if not isinstance(rows, list):
            raise DataFormatError(f"coinbase: unexpected payload {str(rows)[:120]}")
        now = _now(now)
        out = []
        for r in rows:  # [time, low, high, open, close, volume], newest first
            if not isinstance(r, list) or len(r) < 6:
                raise DataFormatError("coinbase: malformed candle row")
            t = utc(r[0])
            out.append(Candle(symbol, AssetClass.CRYPTO, self.name, timeframe, t,
                              open=fnum(r[3], "open", self.name), high=fnum(r[2], "high", self.name),
                              low=fnum(r[1], "low", self.name), close=fnum(r[4], "close", self.name),
                              volume=fnum(r[5], "volume", self.name),
                              is_complete=(t.timestamp() + tf_seconds(timeframe)) <= now.timestamp()))
        out.sort(key=lambda c: c.open_time)
        return out[-limit:]

    def fetch_quote(self, symbol):
        j = self._get_json(f"{self.BASE}/products/{self.map_symbol(symbol)}/ticker")
        ts = parse_iso_utc(j.get("time"))
        return Quote(symbol, AssetClass.CRYPTO, self.name, fnum(j.get("price"), "price", self.name), ts,
                     bid=fnum(j.get("bid"), "bid", self.name), ask=fnum(j.get("ask"), "ask", self.name))


class KrakenProvider(BaseProvider):
    name = "kraken"
    asset_class = AssetClass.CRYPTO
    BASE = "https://api.kraken.com/0/public"
    INTERVAL = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 1440}
    ALIASES = {"BTC": "XBT", "DOGE": "XDG"}

    def map_symbol(self, symbol):
        b, q = _split(symbol)
        return f"{self.ALIASES.get(b, b)}{q}"

    def _result(self, j):
        if not isinstance(j, dict) or j.get("error"):
            err = j.get("error") if isinstance(j, dict) else j
            if err and any("Unknown asset pair" in str(e) for e in err):
                raise SymbolNotSupported(f"kraken: {err}")
            raise DataFormatError(f"kraken: error {err}")
        res = {k: v for k, v in j["result"].items() if k != "last"}
        if len(res) != 1:
            raise DataFormatError("kraken: expected exactly one pair in result")
        return next(iter(res.values()))

    def fetch_candles(self, symbol, timeframe, limit=100, now=None):
        if timeframe not in self.INTERVAL:
            raise SymbolNotSupported(f"kraken: timeframe {timeframe}")
        rows = self._result(self._get_json(f"{self.BASE}/OHLC",
                                           {"pair": self.map_symbol(symbol), "interval": self.INTERVAL[timeframe]}))
        now = _now(now)
        out = []
        for r in rows:  # [time, open, high, low, close, vwap, volume, count], oldest first; last = forming
            t = utc(r[0])
            out.append(Candle(symbol, AssetClass.CRYPTO, self.name, timeframe, t,
                              open=fnum(r[1], "open", self.name), high=fnum(r[2], "high", self.name),
                              low=fnum(r[3], "low", self.name), close=fnum(r[4], "close", self.name),
                              volume=fnum(r[6], "volume", self.name),
                              is_complete=(t.timestamp() + tf_seconds(timeframe)) <= now.timestamp()))
        return out[-limit:]

    def fetch_quote(self, symbol):
        t = self._result(self._get_json(f"{self.BASE}/Ticker", {"pair": self.map_symbol(symbol)}))
        # Kraken ticker has no timestamp; fetch time is used by the engine.
        return Quote(symbol, AssetClass.CRYPTO, self.name, fnum(t["c"][0], "last", self.name), None,
                     bid=fnum(t["b"][0], "bid", self.name), ask=fnum(t["a"][0], "ask", self.name))


class BinanceProvider(BaseProvider):
    name = "binance"
    asset_class = AssetClass.CRYPTO
    BASE = "https://data-api.binance.vision/api/v3"   # public market-data mirror, no key
    INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1d": "1d"}

    def map_symbol(self, symbol):
        b, _ = _split(symbol)
        return f"{b}USDT"

    def fetch_candles(self, symbol, timeframe, limit=100, now=None):
        if timeframe not in self.INTERVAL:
            raise SymbolNotSupported(f"binance: timeframe {timeframe}")
        rows = self._get_json(f"{self.BASE}/klines", {"symbol": self.map_symbol(symbol),
                                                      "interval": self.INTERVAL[timeframe],
                                                      "limit": min(max(limit, 1), 1000)})
        if not isinstance(rows, list):
            raise DataFormatError("binance: unexpected payload")
        now = _now(now)
        out = []
        for r in rows:  # [openTime ms, o, h, l, c, v, closeTime ms, ...]
            t = utc(r[0] / 1000)
            out.append(Candle(symbol, AssetClass.CRYPTO, self.name, timeframe, t,
                              open=fnum(r[1], "open", self.name), high=fnum(r[2], "high", self.name),
                              low=fnum(r[3], "low", self.name), close=fnum(r[4], "close", self.name),
                              volume=fnum(r[5], "volume", self.name),
                              is_complete=(r[6] + 1) / 1000 <= now.timestamp()))
        return out[-limit:]

    def fetch_quote(self, symbol):
        sym = self.map_symbol(symbol)
        bt = self._get_json(f"{self.BASE}/ticker/bookTicker", {"symbol": sym})
        bid, ask = fnum(bt["bidPrice"], "bid", self.name), fnum(bt["askPrice"], "ask", self.name)
        return Quote(symbol, AssetClass.CRYPTO, self.name, (bid + ask) / 2, None, bid=bid, ask=ask,
                     quote_currency="USDT")
