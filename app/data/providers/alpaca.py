"""Alpaca market data (stocks) - second stock source, enabled only with keys.

Uses the read-only market-data host data.alpaca.markets with the free IEX feed.
This module never calls any trading endpoint; paper *execution* (M5) will use
paper-api.alpaca.markets only. Without ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY
the engine does not instantiate this provider (status: needs_credentials).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Secrets
from app.data.calendar import session_at
from app.data.models import AssetClass, Candle, Quote, tf_seconds
from app.data.providers.base import BaseProvider, DataFormatError, SymbolNotSupported, fnum
from app.data.providers.crypto import parse_iso_utc


class AlpacaCredentialsMissing(RuntimeError):
    pass


class AlpacaDataProvider(BaseProvider):
    name = "alpaca"
    asset_class = AssetClass.STOCK
    BASE = "https://data.alpaca.markets/v2/stocks"
    TF = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "1d": "1Day"}
    min_interval_s = 0.35   # free plan: 200 req/min

    def __init__(self, secrets: Secrets, *a, feed: str = "iex", **kw):
        super().__init__(*a, **kw)
        if not (secrets.has("alpaca_api_key_id") and secrets.has("alpaca_api_secret_key")):
            raise AlpacaCredentialsMissing("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set")
        self._key = secrets.alpaca_api_key_id
        self._secret = secrets.alpaca_api_secret_key
        self.feed = feed

    def auth_headers(self) -> dict:
        return {"APCA-API-KEY-ID": self._key.get_secret_value(),
                "APCA-API-SECRET-KEY": self._secret.get_secret_value()}

    def map_symbol(self, symbol):
        s = symbol.upper().strip()
        if not s or "-USD" in s:
            raise SymbolNotSupported(f"alpaca stock data: unsupported {symbol!r}")
        return s

    def fetch_candles(self, symbol, timeframe, limit=100, now=None):
        if timeframe not in self.TF:
            raise SymbolNotSupported(f"alpaca: timeframe {timeframe}")
        now = now or datetime.now(timezone.utc)
        step = tf_seconds(timeframe)
        lookback = timedelta(seconds=step * limit * (4 if timeframe != "1d" else 2)) + timedelta(days=4)
        j = self._get_json(f"{self.BASE}/{self.map_symbol(symbol)}/bars", {
            "timeframe": self.TF[timeframe], "start": (now - lookback).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000, "feed": self.feed, "adjustment": "raw", "sort": "asc"})
        if not isinstance(j, dict) or "bars" not in j:
            raise DataFormatError("alpaca: missing 'bars'")
        out = []
        for b in j["bars"] or []:
            t = parse_iso_utc(b.get("t"))
            if t is None:
                raise DataFormatError("alpaca: bad bar timestamp")
            out.append(Candle(symbol.upper(), AssetClass.STOCK, self.name, timeframe, t,
                              open=fnum(b.get("o"), "o", self.name), high=fnum(b.get("h"), "h", self.name),
                              low=fnum(b.get("l"), "l", self.name), close=fnum(b.get("c"), "c", self.name),
                              volume=fnum(b.get("v"), "v", self.name),
                              is_complete=(t.timestamp() + step) <= now.timestamp(),
                              session=None if timeframe == "1d" else session_at(t)))
        return out[-limit:]

    def fetch_quote(self, symbol):
        j = self._get_json(f"{self.BASE}/{self.map_symbol(symbol)}/quotes/latest", {"feed": self.feed})
        q = j.get("quote") if isinstance(j, dict) else None
        if not q:
            raise DataFormatError("alpaca: missing 'quote'")
        bid, ask = fnum(q.get("bp"), "bp", self.name), fnum(q.get("ap"), "ap", self.name)
        if bid <= 0 or ask <= 0:
            raise DataFormatError("alpaca: empty quote (bid/ask 0)")
        return Quote(symbol.upper(), AssetClass.STOCK, self.name, (bid + ask) / 2,
                     parse_iso_utc(q.get("t")), bid=bid, ask=ask)
