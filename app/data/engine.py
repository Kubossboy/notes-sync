"""MARKET DATA ENGINE: fetch -> normalize -> validate -> label -> cross-check -> persist.

Safe-state policy (DECISIONS.md D24):
* invalid data (malformed payload / OHLC or timestamp violations) -> SAFE STATE
* every configured source of a symbol unreachable -> SAFE STATE, cached data
  is returned labelled CACHED (never tradable)
* cross-source divergence beyond tolerance with no majority -> SAFE STATE
* a single unreachable source with a working fallback -> warning only
* stale data / closed market -> not tradable, no halt
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from app.config import Settings
from app.core.logging_setup import get_logger
from app.core.safety import SafeState
from app.data.crosscheck import CrossCheck, cross_check
from app.data.models import AssetClass, Candle, CandleSeries, DataStatus, Quote, utc
from app.data.providers.base import BaseProvider, DataFormatError, ProviderError, SymbolNotSupported
from app.data.providers.crypto import BinanceProvider, CoinbaseProvider, KrakenProvider
from app.data.providers.yahoo import YahooProvider
from app.data.validation import label_freshness, validate_candles
from app.database.db import Database, utcnow

log = get_logger("data.engine")

PROVIDER_CLASSES = {"yahoo": YahooProvider, "coinbase": CoinbaseProvider,
                    "kraken": KrakenProvider, "binance": BinanceProvider}


def asset_class_of(symbol: str) -> AssetClass:
    return AssetClass.CRYPTO if symbol.upper().endswith("-USD") else AssetClass.STOCK


@dataclass
class SourceAttempt:
    source: str
    ok: bool
    error: str | None = None
    kind: str | None = None        # unreachable | invalid | unsupported


@dataclass
class FetchResult:
    series: CandleSeries
    attempts: list[SourceAttempt] = field(default_factory=list)
    crosscheck: CrossCheck | None = None
    halted: bool = False


class MarketDataEngine:
    def __init__(self, settings: Settings, db: Database, safe_state: SafeState,
                 providers: dict[str, BaseProvider] | None = None,
                 session: requests.Session | None = None):
        self.settings, self.db, self.safe = settings, db, safe_state
        self.cfg = settings.data
        sess = session or requests.Session()
        if providers is None:
            providers = {}
            for name in set(self.cfg.stock_sources) | set(self.cfg.crypto_sources):
                kw = {"include_prepost": self.cfg.include_prepost} if name == "yahoo" else {}
                providers[name] = PROVIDER_CLASSES[name](session=sess, timeout=settings.http.timeout_seconds, **kw)
        self.providers = providers

    def sources_for(self, ac: AssetClass) -> list[str]:
        names = self.cfg.crypto_sources if ac == AssetClass.CRYPTO else self.cfg.stock_sources
        return [n for n in names if n in self.providers]

    # ------------------------------------------------------------ candles
    def get_candles(self, symbol: str, timeframe: str = "5m", limit: int = 100,
                    crosscheck: bool = True, now: datetime | None = None) -> FetchResult:
        symbol = symbol.upper()
        ac = asset_class_of(symbol)
        attempts: list[SourceAttempt] = []
        good: dict[str, CandleSeries] = {}
        for name in self.sources_for(ac):
            series = self._fetch_one(name, symbol, ac, timeframe, limit, attempts, now)
            if series is not None:
                good[name] = series
                if not crosscheck:
                    break
        if not good:
            return self._all_failed(symbol, ac, timeframe, limit, attempts, now)

        primary = good[next(iter(good))]
        result = FetchResult(primary, attempts)
        if len(good) > 1:
            result.crosscheck = self._candle_crosscheck(symbol, ac, good)
            if result.crosscheck and result.crosscheck.outliers:
                primary.warnings.append(f"cross-check outliers: {result.crosscheck.outliers}")
                if primary.source in result.crosscheck.outliers and result.crosscheck.reliable:
                    alt = next(s for n, s in good.items() if n not in result.crosscheck.outliers)
                    alt.warnings.append(f"primary {primary.source} excluded as outlier")
                    result.series = primary = alt
            if result.crosscheck and result.crosscheck.verdict == "divergent":
                result.halted = self._halt(f"cross-source divergence on {symbol} {timeframe}: "
                                           f"{result.crosscheck.deviations_bps}")
        failed = [a.source for a in attempts if not a.ok]
        if failed:
            primary.warnings.append(f"sources failed: {failed}")
        return result

    def _fetch_one(self, name, symbol, ac, timeframe, limit, attempts, now) -> CandleSeries | None:
        prov = self.providers[name]
        fetched_at = datetime.now(timezone.utc)
        try:
            candles = prov.fetch_candles(symbol, timeframe, limit, now=now)
        except SymbolNotSupported as e:
            attempts.append(SourceAttempt(name, False, str(e), "unsupported"))
            return None
        except DataFormatError as e:
            attempts.append(SourceAttempt(name, False, str(e), "invalid"))
            self.db.log_error(f"data.{name}", e)
            self._halt(f"invalid payload from {name} for {symbol}: {e}")
            return None
        except ProviderError as e:
            attempts.append(SourceAttempt(name, False, str(e), "unreachable"))
            self.db.log_event("source_unreachable", f"data.{name}", f"{symbol}: {e}", "warning")
            return None
        series = CandleSeries(symbol, ac, name, timeframe, candles, fetched_at)
        issues, warns = validate_candles(candles, timeframe, ac, self.cfg, now)
        if getattr(prov, "last_skipped_nulls", 0):
            warns.append(f"{prov.last_skipped_nulls} null bar(s) skipped")
        series.issues, series.warnings = issues, warns
        if issues:
            attempts.append(SourceAttempt(name, False, "; ".join(issues[:3]), "invalid"))
            self.db.log_event("invalid_market_data", f"data.{name}", f"{symbol} {timeframe}",
                              "error", {"issues": issues[:20]})
            if issues == ["no candles returned"]:
                return None   # empty answer = missing data, handled by fallback/all-failed path
            self._halt(f"invalid candles from {name} for {symbol}: {issues[0]}")
            return None
        label_freshness(series, self.cfg, now)
        self.persist_candles(series)
        attempts.append(SourceAttempt(name, True))
        return series

    def _candle_crosscheck(self, symbol, ac, good: dict[str, CandleSeries]) -> CrossCheck | None:
        # compare closes of the latest complete bar that every source has
        common = None
        for s in good.values():
            ts = {c.open_time for c in s.complete}
            common = ts if common is None else common & ts
        if not common:
            return None
        t = max(common)
        prices = {n: next(c.close for c in s.complete if c.open_time == t) for n, s in good.items()}
        cc = cross_check(symbol, ac, prices, self.cfg, kind="candle")
        self._store_crosscheck(cc)
        return cc

    def _all_failed(self, symbol, ac, timeframe, limit, attempts, now) -> FetchResult:
        cached = self.load_cached(symbol, timeframe, limit)
        series = CandleSeries(symbol, ac, cached[0].source if cached else "none", timeframe, cached,
                              datetime.now(timezone.utc))
        label_freshness(series, self.cfg, now, from_cache=bool(cached))
        series.warnings.append("all sources failed: " + "; ".join(
            f"{a.source}={a.kind}" for a in attempts) if attempts else "no sources configured")
        only_unsupported = attempts and all(a.kind == "unsupported" for a in attempts)
        halted = False
        if not only_unsupported:
            halted = self._halt(f"no valid market data for {symbol} {timeframe} from any source")
        return FetchResult(series, attempts, halted=halted)

    # ------------------------------------------------------------ quotes
    def get_quote_consensus(self, symbol: str) -> tuple[CrossCheck, list[Quote], list[SourceAttempt]]:
        symbol = symbol.upper()
        ac = asset_class_of(symbol)
        quotes, attempts = [], []
        for name in self.sources_for(ac):
            try:
                q = self.providers[name].fetch_quote(symbol)
            except DataFormatError as e:
                attempts.append(SourceAttempt(name, False, str(e), "invalid"))
                self.db.log_error(f"data.{name}", e)
                self._halt(f"invalid quote from {name} for {symbol}: {e}")
                continue
            except ProviderError as e:
                attempts.append(SourceAttempt(name, False, str(e),
                                              "unsupported" if isinstance(e, SymbolNotSupported) else "unreachable"))
                continue
            if q.bid and q.ask and q.ask < q.bid:
                attempts.append(SourceAttempt(name, False, "crossed book (ask < bid)", "invalid"))
                self._halt(f"crossed quote from {name} for {symbol}")
                continue
            quotes.append(q)
            attempts.append(SourceAttempt(name, True))
            self._persist_quote(q)
        cc = cross_check(symbol, ac, {q.source: q.price for q in quotes}, self.cfg, kind="quote")
        self._store_crosscheck(cc)
        if cc.verdict == "divergent":
            self._halt(f"quote divergence on {symbol}: {cc.deviations_bps}")
        elif cc.verdict == "unavailable" and not all(a.kind == "unsupported" for a in attempts):
            self._halt(f"no quote for {symbol} from any source")
        return cc, quotes, attempts

    # ------------------------------------------------------------ persistence
    def persist_candles(self, s: CandleSeries) -> int:
        n = 0
        status = s.status.value if s.status != DataStatus.UNAVAILABLE else "delayed"
        with self.db.transaction() as c:
            for k in s.candles:
                cur = c.execute(
                    "INSERT INTO market_observations (observed_ts, source_ts, symbol, asset_class, source, "
                    "data_status, timeframe, open, high, low, close, volume, is_complete_candle, session, kind) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'candle') "
                    "ON CONFLICT(source, symbol, timeframe, source_ts) WHERE kind='candle' DO UPDATE SET "
                    "observed_ts=excluded.observed_ts, open=excluded.open, high=excluded.high, low=excluded.low, "
                    "close=excluded.close, volume=excluded.volume, is_complete_candle=excluded.is_complete_candle, "
                    "data_status=excluded.data_status "
                    "WHERE market_observations.is_complete_candle = 0",   # finalized bars are never rewritten
                    (s.fetched_at.isoformat(), k.open_time.isoformat(), k.symbol, k.asset_class.value,
                     k.source, status, k.timeframe, k.open, k.high, k.low, k.close, k.volume,
                     int(k.is_complete), k.session))
                n += cur.rowcount
        return n

    def _persist_quote(self, q: Quote) -> None:
        self.db.insert("market_observations", {
            "observed_ts": utcnow(), "source_ts": q.source_ts.isoformat() if q.source_ts else None,
            "symbol": q.symbol, "asset_class": q.asset_class.value, "source": q.source,
            "data_status": "live" if q.asset_class == AssetClass.CRYPTO else "delayed",
            "timeframe": "quote", "close": q.price, "bid": q.bid, "ask": q.ask,
            "quote_currency": q.quote_currency, "kind": "quote"})

    def _store_crosscheck(self, cc: CrossCheck) -> None:
        self.db.insert("crosscheck_results", {
            "ts": utcnow(), "symbol": cc.symbol, "asset_class": cc.asset_class.value, "kind": cc.kind,
            "consensus_price": cc.consensus, "sources_json": json.dumps(
                {k: {"price": v, "dev_bps": cc.deviations_bps.get(k)} for k, v in cc.prices.items()}),
            "max_deviation_bps": cc.max_deviation_bps, "outliers": ",".join(cc.outliers) or None,
            "verdict": cc.verdict})

    def load_cached(self, symbol: str, timeframe: str, limit: int = 100) -> list[Candle]:
        rows = self.db.conn.execute(
            "SELECT * FROM market_observations WHERE kind='candle' AND symbol=? AND timeframe=? "
            "AND is_complete_candle=1 ORDER BY source_ts DESC, id DESC LIMIT ?", (symbol, timeframe, limit * 3)).fetchall()
        if not rows:
            return []
        # one source only (never mix sources in a series), preferring configured priority order
        have = {r["source"] for r in rows}
        prio = [n for n in self.sources_for(AssetClass(rows[0]["asset_class"])) if n in have]
        src = prio[0] if prio else rows[0]["source"]
        rows = [r for r in rows if r["source"] == src][:limit]
        return [Candle(r["symbol"], AssetClass(r["asset_class"]), r["source"], r["timeframe"],
                       datetime.fromisoformat(r["source_ts"]), r["open"], r["high"], r["low"], r["close"],
                       r["volume"], True, r["session"]) for r in reversed(rows)]

    # ------------------------------------------------------------ safety
    def _halt(self, reason: str) -> bool:
        log.error("market data problem: %s", reason)
        self.safe.halt(reason, module="data")
        return True

    # ------------------------------------------------------------ batch
    def collect(self, symbols: list[str], timeframe: str = "5m", limit: int = 100) -> list[dict]:
        report = []
        for sym in symbols:
            try:
                r = self.get_candles(sym, timeframe, limit)
                cc, _, qa = self.get_quote_consensus(sym)
            except Exception as e:   # unexpected bug -> journal + safe state, keep going
                self.db.log_error("data.engine", e)
                self._halt(f"unexpected error collecting {sym}: {type(e).__name__}: {e}")
                report.append({"symbol": sym, "status": "error", "error": str(e)})
                continue
            s = r.series
            last = s.last_complete
            report.append({
                "symbol": sym, "asset_class": s.asset_class.value, "source": s.source,
                "status": s.status.value, "tradable": s.tradable, "stale": s.stale,
                "market_open": s.market_open, "bars": len(s.candles), "complete_bars": len(s.complete),
                "last_bar_utc": last.open_time.isoformat() if last else None,
                "last_close": last.close if last else None, "age_s": s.age_seconds,
                "forming_bar": bool(s.candles and not s.candles[-1].is_complete),
                "candle_check": r.crosscheck.verdict if r.crosscheck else None,
                "candle_dev_bps": r.crosscheck.deviations_bps if r.crosscheck else None,
                "quote_consensus": cc.consensus, "quote_check": cc.verdict,
                "quote_dev_bps": cc.deviations_bps,
                "sources_failed": [a.source for a in r.attempts + qa if not a.ok],
                "warnings": s.warnings, "halted": r.halted,
            })
        self.db.log_event("collect", "data.engine", f"collected {len(symbols)} symbols ({timeframe})",
                          details={"ok": sum(1 for x in report if x.get("status") not in ("error", "unavailable"))})
        return report
