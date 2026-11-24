"""Source verification, de-duplication and story clustering."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

_STOP = set("""a an the of to in on for and or with at by from as is are was be its it this that after over
new says said report reports amid into up down vs inc corp co ltd""".split())
_TOK = re.compile(r"[a-z0-9]+")


def normalize_title(t: str) -> str:
    return " ".join(_TOK.findall(t.lower()))


def content_hash(source: str, title: str, url: str | None) -> str:
    return hashlib.sha256(f"{source}|{normalize_title(title)}|{(url or '').split('?')[0]}".encode()).hexdigest()[:32]


def tokens(t: str) -> set[str]:
    return {w for w in _TOK.findall(t.lower()) if w not in _STOP and len(w) > 2}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def same_story(a_title: str, a_ts: datetime | None, b_title: str, b_ts: datetime | None,
               threshold: float = 0.45, window_h: float = 36) -> bool:
    if a_ts and b_ts and abs((a_ts - b_ts).total_seconds()) > window_h * 3600:
        return False
    return similarity(a_title, b_title) >= threshold


def verification_status(tier: int, independent_sources: int) -> tuple[bool, str]:
    """Verified = primary source, or corroborated by >= 2 independent non-social sources."""
    if tier == 1:
        return True, "primary_source"
    if independent_sources >= 2 and tier <= 3:
        return True, f"corroborated_by_{independent_sources}_sources"
    if tier == 4:
        return False, "social_unverified"
    return False, "single_source"


def time_quality(published: datetime | None, fetched: datetime, max_age_h: float = 72) -> tuple[float, list[str]]:
    flags = []
    if published is None:
        return 0.6, ["published_time_missing"]
    if published > fetched + timedelta(minutes=10):
        return 0.0, ["published_time_in_future"]
    age = (fetched - published).total_seconds() / 3600
    if age > max_age_h:
        flags.append("old_news")
        return 0.5, flags
    return 1.0, flags
