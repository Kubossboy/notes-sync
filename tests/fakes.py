"""Test doubles. Payloads here are synthetic fixtures shaped like each API's
documented response format - they are NOT market data."""
import json
from datetime import datetime, timedelta, timezone

from app.data.models import AssetClass, Candle, Quote
from app.data.providers.base import BaseProvider, DataFormatError, ProviderError


class FakeResp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code, self._payload = status, payload
        self.text = text if text is not None else json.dumps(payload)

    @property
    def content(self):
        return self.text.encode("utf-8") if isinstance(self.text, str) else self.text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class SeqSession:
    """Returns queued responses in order; records requested URLs."""
    def __init__(self, *resps):
        self.resps, self.calls = list(resps), []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        r = self.resps.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def bars(symbol, n=10, tf_s=300, end=None, start_price=100.0, ac=AssetClass.CRYPTO,
         source="fake", forming=True, session=None):
    end = end or datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)
    out = []
    p = start_price
    for i in range(n):
        t = end - timedelta(seconds=tf_s * (n - 1 - i))
        o, c = p, p * 1.001
        out.append(Candle(symbol, ac, source, {60: "1m", 300: "5m"}[tf_s], t, o, max(o, c) * 1.0005,
                          min(o, c) * 0.9995, c, 10.0, is_complete=not (forming and i == n - 1),
                          session=session))
        p = c
    return out


class FakeProvider(BaseProvider):
    def __init__(self, name, candles=None, exc=None, price=None, bid=None, ask=None, quote_exc=None):
        super().__init__()
        self.name = name
        self._candles, self._exc = candles, exc
        self._price, self._bid, self._ask, self._qexc = price, bid, ask, quote_exc
        self.calls = 0

    def map_symbol(self, s):
        return s

    def fetch_candles(self, symbol, timeframe, limit=100, now=None):
        self.calls += 1
        if self._exc:
            raise self._exc
        return [c.__class__(**{**c.__dict__, "source": self.name}) for c in (self._candles or [])][-limit:]

    def fetch_quote(self, symbol):
        if self._qexc:
            raise self._qexc
        ac = AssetClass.CRYPTO if symbol.endswith("-USD") else AssetClass.STOCK
        return Quote(symbol, ac, self.name, self._price, None, self._bid, self._ask)
