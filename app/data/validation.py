"""Candle validation and freshness labelling.

issues   -> data-integrity violations: the series is rejected (and SAFE STATE)
warnings -> worth recording but not invalid (gaps, spikes, skipped nulls)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import DataSection
from app.data.calendar import session_at
from app.data.models import AssetClass, Candle, CandleSeries, DataStatus, grid_seconds, tf_seconds

FUTURE_TOLERANCE_S = 120  # exchange / box clock skew; still flags bars minutes or hours ahead


def as_utc(dt: datetime) -> datetime:
    """Timezone-aware UTC. Naive values are treated as already-UTC (backtest convention), not local."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def wall_clock() -> datetime:
    """Fresh UTC wall clock. Never cache this across a sleep or a long cycle."""
    return datetime.now(timezone.utc)


def as_of_lagged_behind_clock(asof: datetime, clock: datetime, candles: list[Candle],
                              tol_s: float = FUTURE_TOLERANCE_S) -> bool:
    """True when `asof` is stale (long cycle or VM sleep) *and* the series contains
    bars that already existed by `clock` but look 'in the future' vs the stale as-of.
    Historical backtests (all bars at or before asof) return False, so their as-of is kept."""
    asof, clock = as_utc(asof), as_utc(clock)
    if asof >= clock - timedelta(seconds=tol_s):
        return False
    for c in candles:
        ot = as_utc(c.open_time)
        if ot > asof + timedelta(seconds=tol_s) and ot <= clock + timedelta(seconds=tol_s):
            return True
    return False


def effective_as_of(now: datetime | None, clock: datetime | None = None,
                    candles: list[Candle] | None = None) -> datetime:
    """As-of instant used to judge timestamps and bar completeness.
    If `clock` is omitted and `now` is given, clock defaults to `now` (deterministic tests /
    backtests). Live callers must pass a fresh `clock=wall_clock()` so a stale cycle-start
    `now` after sleep is replaced by the wall clock."""
    if clock is None:
        clock = now if now is not None else wall_clock()
    clock = as_utc(clock)
    asof = as_utc(now) if now is not None else clock
    if candles and as_of_lagged_behind_clock(asof, clock, candles):
        return clock
    return asof


def retag_completeness(candles: list[Candle], timeframe: str, asof: datetime) -> list[Candle]:
    """Recompute is_complete from a (possibly refreshed) as-of, after a stale now marked
    already-closed bars as forming and then tripped 'incomplete candle is not the last one'."""
    from dataclasses import replace
    step = tf_seconds(timeframe)
    asof = as_utc(asof)
    out = []
    for c in candles:
        should = (as_utc(c.open_time) + timedelta(seconds=step)) <= asof
        out.append(c if c.is_complete == should else replace(c, is_complete=should))
    return out


def validate_candles(candles: list[Candle], timeframe: str, asset_class: AssetClass,
                     cfg: DataSection, now: datetime | None = None,
                     clock: datetime | None = None) -> tuple[list[str], list[str]]:
    now = effective_as_of(now, clock, candles)
    issues: list[str] = []
    warnings: list[str] = []
    if not candles:
        return ["no candles returned"], warnings
    step = tf_seconds(timeframe)
    grid = grid_seconds(timeframe, asset_class)
    prev: Candle | None = None
    gaps = 0
    for i, c in enumerate(candles):
        tag = f"{c.source}:{c.symbol}@{c.open_time.isoformat()}"
        if c.timeframe != timeframe:
            issues.append(f"{tag} timeframe mismatch {c.timeframe}")
        if min(c.open, c.high, c.low, c.close) <= 0:
            issues.append(f"{tag} non-positive price")
        if c.high < max(c.open, c.close) - 1e-12 or c.low > min(c.open, c.close) + 1e-12 or c.high < c.low:
            issues.append(f"{tag} OHLC inconsistent (o={c.open} h={c.high} l={c.low} c={c.close})")
        if c.volume < 0:
            issues.append(f"{tag} negative volume")
        # Compare timezone-aware UTC instants. Bars that already existed by the
        # (possibly refreshed) as-of are stale/old, never "future".
        if as_utc(c.open_time) > now + timedelta(seconds=FUTURE_TOLERANCE_S):
            issues.append(f"{tag} timestamp in the future")
        if c.is_complete and timeframe != "1d" and int(c.open_time.timestamp()) % grid != 0:
            issues.append(f"{tag} complete candle not aligned to {grid}s grid")
        if not c.is_complete and i != len(candles) - 1:
            issues.append(f"{tag} incomplete candle is not the last one")
        if prev is not None:
            dt = (c.open_time - prev.open_time).total_seconds()
            if dt <= 0:
                issues.append(f"{tag} duplicate or out-of-order timestamp")
            elif timeframe != "1d" and dt > step:
                # crypto trades 24/7 -> any gap is notable; stocks only within one regular session
                same_session = (asset_class == AssetClass.STOCK and c.session == prev.session == "regular"
                                and c.open_time.date() == prev.open_time.date())
                if asset_class == AssetClass.CRYPTO or same_session:
                    gaps += int(dt // step) - 1
            if prev.close > 0:
                chg = abs(c.close / prev.close - 1) * 100
                if chg > cfg.max_bar_change_pct:
                    warnings.append(f"{tag} price spike {chg:.1f}% vs previous bar")
        prev = c
    if gaps:
        warnings.append(f"{gaps} missing bar(s) in series")
    return issues, warnings


def label_freshness(series: CandleSeries, cfg: DataSection, now: datetime | None = None,
                    from_cache: bool = False, clock: datetime | None = None) -> CandleSeries:
    now = effective_as_of(now, clock, series.candles)
    last = series.last_complete
    if not series.candles or last is None:
        series.status = DataStatus.UNAVAILABLE
        series.age_seconds = None
        return series
    step = tf_seconds(series.timeframe)
    age = (now - last.close_time).total_seconds()
    series.age_seconds = round(age, 1)
    if series.asset_class == AssetClass.CRYPTO:
        series.market_open = True
    else:
        sess = session_at(now)
        allowed = {"regular", "pre", "post"} if cfg.include_prepost else {"regular"}
        series.market_open = sess in allowed
    if series.timeframe == "1d":
        live_limit, stale_limit = 2 * 86400, 4 * 86400   # weekends/holidays
    else:
        live_limit = cfg.live_max_age_bars * step + cfg.live_grace_seconds
        stale_limit = cfg.stale_after_bars * step + cfg.live_grace_seconds
    if from_cache:
        series.status = DataStatus.CACHED
    elif not series.market_open:
        series.status = DataStatus.DELAYED
        series.warnings.append("market closed: last bar is from the previous session")
    else:
        series.status = DataStatus.LIVE if age <= live_limit else DataStatus.DELAYED
    series.stale = bool(series.market_open and age > stale_limit)
    if series.stale:
        series.warnings.append(f"stale: last complete bar closed {age:.0f}s ago while market open")
    return series
