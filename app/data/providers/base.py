"""Provider base class: safe HTTP (URL guard, timeout, bounded retries, rate limit)."""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime

import requests

from app.core.logging_setup import get_logger
from app.core.safety import assert_url_allowed
from app.data.models import AssetClass, Candle, Quote

log = get_logger("data.provider")


class ProviderError(RuntimeError):
    """Source failed (network, HTTP, rate limit). Caller may fall back."""


class DataFormatError(ProviderError):
    """Source answered but the payload is malformed -> data integrity problem."""


class SymbolNotSupported(ProviderError):
    pass


class BaseProvider(ABC):
    name: str = "base"
    asset_class: AssetClass
    min_interval_s: float = 0.2      # polite rate limit between requests
    retries: int = 2
    backoff_s: float = 0.8

    def __init__(self, session: requests.Session | None = None, timeout: float = 10.0,
                 user_agent: str = "ATLAS-Trading-AI/0.2"):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.user_agent = user_agent
        self._last = 0.0
        self._lock = threading.Lock()

    # ---- HTTP ----
    def _get_json(self, url: str, params: dict | None = None):
        assert_url_allowed(url)
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            with self._lock:
                wait = self.min_interval_s - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                r = self.session.get(url, params=params, timeout=self.timeout,
                                     headers={"User-Agent": self.user_agent, "Accept": "application/json"})
            except requests.RequestException as e:
                last_exc = ProviderError(f"{self.name}: {type(e).__name__}: {str(e)[:160]}")
            else:
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError as e:
                        raise DataFormatError(f"{self.name}: non-JSON response") from e
                if r.status_code in (400, 404):
                    raise SymbolNotSupported(f"{self.name}: HTTP {r.status_code} for {url}")
                last_exc = ProviderError(f"{self.name}: HTTP {r.status_code}")
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
            if attempt < self.retries:
                time.sleep(self.backoff_s * (2 ** attempt))
        raise last_exc or ProviderError(f"{self.name}: unknown failure")

    # ---- API ----
    @abstractmethod
    def map_symbol(self, symbol: str) -> str: ...

    @abstractmethod
    def fetch_candles(self, symbol: str, timeframe: str, limit: int = 100,
                      now: datetime | None = None) -> list[Candle]: ...

    @abstractmethod
    def fetch_quote(self, symbol: str) -> Quote: ...


def fnum(v, field: str, src: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise DataFormatError(f"{src}: non-numeric {field}={v!r}")
    if f != f or f in (float("inf"), float("-inf")):
        raise DataFormatError(f"{src}: non-finite {field}")
    return f
