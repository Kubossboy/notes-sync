"""Candle validation and freshness labelling.

issues   -> data-integrity violations: the series is rejected (and SAFE STATE)
warnings -> worth recording but not invalid (gaps, spikes, skipped nulls)
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import DataSection
from app.data.calendar import session_at
from app.data.models import AssetClass, Candle, CandleSeries, DataStatus, grid_seconds, tf_seconds

FUTURE_TOLERANCE_S = 60


def validate_candles(candles: list[Candle], timeframe: str, asset_class: AssetClass,
                     cfg: DataSection, now: datetime | None = None) -> tuple[list[str], list[str]]:
    now = now or datetime.now(timezone.utc)
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
        if c.open_time.timestamp() > now.timestamp() + FUTURE_TOLERANCE_S:
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
                    from_cache: bool = False) -> CandleSeries:
    now = now or datetime.now(timezone.utc)
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
