"""Real historical bars from free sources, cached under var/history (git-ignored, rebuildable).

* crypto: Coinbase Exchange public candles (USD pairs, same primary source as live), 300 bars/request
* stocks: Alpaca market data (free IEX feed, split-adjusted), paginated; needs the Alpaca paper keys
  (read-only market-data host). Caveat: IEX volume is only a fraction of consolidated volume.
* Yahoo 5m (consolidated volume, ~60 days max) as a cross-source robustness set for stocks.
All timestamps UTC; bars are bar-open times; every returned bar is COMPLETE (history only).
"""
from __future__ import annotations

import gzip
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from app.config import PROJECT_ROOT, Secrets
from app.core.safety import assert_url_allowed
from app.data.calendar import session_at

CACHE = PROJECT_ROOT / "var" / "history"
UA = "ATLAS-Trading-AI/0.6 (backtest research)"
COLS = ["open", "high", "low", "close", "volume"]


def _get(session, url, params=None, headers=None, tries=4):
    assert_url_allowed(url)
    for i in range(tries):
        r = session.get(url, params=params, headers={"User-Agent": UA, **(headers or {})}, timeout=20)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(1.5 * (i + 1))
            continue
        raise ConnectionError(f"{url}: HTTP {r.status_code} {r.text[:150]}")
    raise ConnectionError(f"{url}: retries exhausted")


def coinbase_history(symbol: str, tf: str, start: datetime, end: datetime, session=None) -> pd.DataFrame:
    gran = {"5m": 300, "15m": 900, "1h": 3600}[tf]
    s = session or requests.Session()
    url = f"https://api.exchange.coinbase.com/products/{symbol}/candles"
    rows, t = [], start
    while t < end:
        t2 = min(end, t + timedelta(seconds=gran * 300))
        j = _get(s, url, {"granularity": gran, "start": t.isoformat(), "end": t2.isoformat()})
        rows += j
        t = t2
        time.sleep(0.15)                                  # ~6 req/s, under the public limit
    df = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
    df.index = pd.to_datetime(df.pop("t"), unit="s", utc=True)
    return _finish(df, start, end, gran)


def alpaca_history(symbol: str, tf: str, start: datetime, end: datetime, secrets: Secrets,
                   session=None) -> pd.DataFrame:
    if not (secrets.has("alpaca_api_key_id") and secrets.has("alpaca_api_secret_key")):
        raise PermissionError("needs_credentials: Alpaca keys (read-only market data)")
    s = session or requests.Session()
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    hdr = {"APCA-API-KEY-ID": secrets.alpaca_api_key_id.get_secret_value(),
           "APCA-API-SECRET-KEY": secrets.alpaca_api_secret_key.get_secret_value()}
    params = {"timeframe": {"5m": "5Min", "15m": "15Min", "1m": "1Min"}[tf], "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000, "feed": "iex", "adjustment": "split",
              "sort": "asc"}
    rows = []
    while True:
        j = _get(s, url, params, hdr)
        rows += j.get("bars") or []
        tok = j.get("next_page_token")
        if not tok:
            break
        params["page_token"] = tok
        time.sleep(0.35)                                  # free plan: 200 req/min
    df = pd.DataFrame(rows).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df.index = pd.to_datetime(df.pop("t"), utc=True)
    return _finish(df, start, end, {"5m": 300, "15m": 900, "1m": 60}[tf])


def yahoo_history(symbol: str, tf: str = "5m", session=None) -> pd.DataFrame:
    s = session or requests.Session()
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    j = _get(s, url, {"interval": tf, "range": "60d", "includePrePost": "true"})
    r = j["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    df = pd.DataFrame({k: q[k] for k in COLS}, index=pd.to_datetime(r["timestamp"], unit="s", utc=True))
    step = {"5m": 300, "15m": 900}[tf]
    secs = ((df.index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).astype("int64")
    df = df[secs % step == 0]                                          # drop off-grid snapshot rows
    now = datetime.now(timezone.utc)
    return _finish(df.dropna(), df.index.min(), now - timedelta(seconds=step), step)


def _finish(df: pd.DataFrame, start, end, step: int) -> pd.DataFrame:
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df.index >= pd.Timestamp(start)) & (df.index + pd.Timedelta(seconds=step) <= pd.Timestamp(end))]
    df = df[COLS].astype(float)
    bad = (df["high"] < df[["open", "close", "low"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))
    df = df[~bad & (df["close"] > 0)]
    return df


def with_sessions(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["session"] = [session_at(t.to_pydatetime()) for t in df.index]
    return df


def load(source: str, symbol: str, tf: str, days: int, secrets: Secrets | None = None,
         refresh: bool = False, max_age_hours: float = 24) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{source}_{symbol}_{tf}_{days}d.csv.gz"
    if f.exists() and not refresh and (time.time() - f.stat().st_mtime) < max_age_hours * 3600:
        df = pd.read_csv(f, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)
        return df
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    if source == "coinbase":
        df = coinbase_history(symbol, tf, start, end)
    elif source == "alpaca":
        df = with_sessions(alpaca_history(symbol, tf, start, end, secrets or Secrets()))
    elif source == "yahoo":
        df = with_sessions(yahoo_history(symbol, tf))
    else:
        raise ValueError(source)
    with gzip.open(f, "wt") as fh:
        df.to_csv(fh)
    return df
