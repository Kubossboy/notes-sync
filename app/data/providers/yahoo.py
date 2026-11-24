"""Yahoo Finance chart endpoint (free, unofficial, keyless) for US stocks.

Limitations (documented, not hidden): unofficial API that may change or
rate-limit; no bid/ask in the chart payload; Yahoo appends a partial
"current" bar whose timestamp is not on the interval grid - it is marked
incomplete. Null OHLC entries (no trades) are skipped and reported as gaps.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.data.calendar import session_at
from app.data.models import AssetClass, Candle, Quote, grid_seconds, tf_seconds, utc
from app.data.providers.base import BaseProvider, DataFormatError, SymbolNotSupported, fnum


class YahooProvider(BaseProvider):
    name = "yahoo"
    asset_class = AssetClass.STOCK
    BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
    INTERVAL = {"1m": ("1m", "1d"), "5m": ("5m", "5d"), "15m": ("15m", "5d"),
                "1h": ("60m", "1mo"), "1d": ("1d", "1y")}
    min_interval_s = 0.5

    def __init__(self, *a, include_prepost: bool = True, **kw):
        super().__init__(*a, **kw)
        self.include_prepost = include_prepost
        self.user_agent = "Mozilla/5.0 (X11; Linux x86_64) ATLAS-Trading-AI/0.2"
        self.last_skipped_nulls = 0

    def map_symbol(self, symbol):
        s = symbol.upper().strip()
        if not s or "-USD" in s or "/" in s:
            raise SymbolNotSupported(f"yahoo provider is for US stocks only, got {symbol!r}")
        return s.replace(".", "-")  # BRK.B -> BRK-B

    def _chart(self, symbol, timeframe):
        if timeframe not in self.INTERVAL:
            raise SymbolNotSupported(f"yahoo: timeframe {timeframe}")
        interval, rng = self.INTERVAL[timeframe]
        j = self._get_json(f"{self.BASE}/{self.map_symbol(symbol)}",
                           {"interval": interval, "range": rng,
                            "includePrePost": "true" if self.include_prepost and timeframe != "1d" else "false"})
        chart = j.get("chart", {}) if isinstance(j, dict) else {}
        if chart.get("error"):
            raise SymbolNotSupported(f"yahoo: {chart['error']}")
        try:
            return chart["result"][0]
        except (KeyError, IndexError, TypeError):
            raise DataFormatError("yahoo: missing chart.result")

    def fetch_candles(self, symbol, timeframe, limit=100, now=None):
        res = self._chart(symbol, timeframe)
        now = now or datetime.now(timezone.utc)
        ts = res.get("timestamp") or []
        try:
            q = res["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError):
            if not ts:
                return []
            raise DataFormatError("yahoo: missing indicators.quote")
        step = tf_seconds(timeframe)
        grid = grid_seconds(timeframe, AssetClass.STOCK)
        out, skipped = [], 0
        for i, t in enumerate(ts):
            vals = [q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close")]
            if any(v is None for v in vals):
                skipped += 1
                continue
            vol = q.get("volume", [None] * len(ts))[i]
            ot = utc(t)
            on_grid = timeframe == "1d" or t % grid == 0
            # a later on-grid bar means this one ended (session-truncated bars, e.g. 14:00-14:30 UTC)
            nxt = next((u for u in ts[i + 1:] if u % grid == 0), None)
            complete = on_grid and ((t + step) <= now.timestamp() or (nxt is not None and nxt > t))
            if timeframe == "1d":
                complete = ot.date() < now.date() or session_at(now) in ("post", "closed")
            o, h, l, c = (round(fnum(v, k, self.name), 6) for v, k in zip(vals, ("open", "high", "low", "close")))
            out.append(Candle(symbol, AssetClass.STOCK, self.name, timeframe, ot,
                              open=o, high=h, low=l, close=c,
                              volume=fnum(vol or 0, "volume", self.name), is_complete=complete,
                              session=None if timeframe == "1d" else session_at(ot)))
        self.last_skipped_nulls = skipped
        return out[-limit:]

    def fetch_quote(self, symbol):
        res = self._chart(symbol, "1m")
        m = res.get("meta", {})
        price = m.get("regularMarketPrice")
        t = m.get("regularMarketTime")
        return Quote(symbol, AssetClass.STOCK, self.name, fnum(price, "regularMarketPrice", self.name),
                     utc(t) if t else None)
