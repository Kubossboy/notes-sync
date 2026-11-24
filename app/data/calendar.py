"""US equity market calendar (NYSE/Nasdaq) for regular, pre- and post-market sessions.

Holidays/early closes are hard-coded from the published NYSE calendar for
2024-2027 (2024 added in M6 for 2-year backtests). Outside that range `is_trading_day` raises, so the system cannot
silently assume a session exists (fail closed).
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

NYSE_HOLIDAYS = {
    # 2024
    date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29), date(2024, 5, 27),
    date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2), date(2024, 11, 28), date(2024, 12, 25),
    # 2025
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27),
    date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}
EARLY_CLOSES = {  # 13:00 ET close
    date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24),
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
}
KNOWN_YEARS = {2024, 2025, 2026, 2027}

PRE_OPEN, REG_OPEN, REG_CLOSE, EARLY_CLOSE, POST_CLOSE = (
    time(4, 0), time(9, 30), time(16, 0), time(13, 0), time(20, 0))


class CalendarUnknown(RuntimeError):
    pass


def is_trading_day(d: date) -> bool:
    if d.year not in KNOWN_YEARS:
        raise CalendarUnknown(f"no NYSE holiday data for {d.year}; update app/data/calendar.py")
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def session_at(ts: datetime) -> str:
    """Return 'pre', 'regular', 'post' or 'closed' for a UTC/aware timestamp."""
    et = ts.astimezone(ET)
    if not is_trading_day(et.date()):
        return "closed"
    t = et.time()
    close = EARLY_CLOSE if et.date() in EARLY_CLOSES else REG_CLOSE
    post_close = time(17, 0) if et.date() in EARLY_CLOSES else POST_CLOSE
    if PRE_OPEN <= t < REG_OPEN:
        return "pre"
    if REG_OPEN <= t < close:
        return "regular"
    if close <= t < post_close:
        return "post"
    return "closed"


def regular_session_open(ts: datetime) -> bool:
    return session_at(ts) == "regular"


def regular_close_at(ts: datetime) -> datetime | None:
    """Regular-session close (aware UTC) for the ET trading day of `ts`; None if not a trading day."""
    from datetime import timezone
    d = ts.astimezone(ET).date()
    if not is_trading_day(d):
        return None
    t = EARLY_CLOSE if d in EARLY_CLOSES else REG_CLOSE
    return datetime.combine(d, t, tzinfo=ET).astimezone(timezone.utc)


def first_session_close_after(ts: datetime, max_days: int = 10) -> datetime | None:
    """Close (UTC) of the first regular session that ends after `ts` (same day if before/during the session)."""
    from datetime import timedelta, timezone
    d = ts.astimezone(ET).date()
    for _ in range(max_days):
        if d.year in KNOWN_YEARS and is_trading_day(d):
            close = datetime.combine(d, EARLY_CLOSE if d in EARLY_CLOSES else REG_CLOSE, tzinfo=ET)
            if close > ts:
                return close.astimezone(timezone.utc)
        d = d + timedelta(days=1)
    return None


def catalyst_fresh(published: datetime, now: datetime, hours: float, session_aware: bool = True,
                   max_hours: float = 96) -> bool:
    """Flat window OR (stocks) until the close of the first regular session after publication, so an
    after-close earnings release / 8-K is still eligible at the next open. Never in the future."""
    age_h = (now - published).total_seconds() / 3600
    if age_h < 0 or age_h > max_hours:
        return False
    if age_h <= hours:
        return True
    if not session_aware:
        return False
    close = first_session_close_after(published)
    return close is not None and now <= close


def trading_day_of(ts: datetime) -> date:
    """ATLAS accounting day = America/New_York calendar date (also for 24/7 crypto)."""
    return ts.astimezone(ET).date()
