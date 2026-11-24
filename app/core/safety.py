"""Safety layer: paper-only enforcement and the persistent SAFE STATE.

SAFE STATE (spec section 15): when data, integrations, the database or the
risk engine misbehave, ATLAS halts new simulated trades until a human (or a
later explicit check) resumes it. The flag lives in the database so it
survives restarts.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import FORBIDDEN_LIVE_ENDPOINTS, Settings
from app.core.logging_setup import get_logger
from app.database.db import Database, utcnow

log = get_logger("safety")
SAFE_STATE_KEY = "safe_state"


class LiveTradingForbidden(RuntimeError):
    """Raised whenever anything tries to reach a live/real-money endpoint."""


def assert_paper_only(settings: Settings) -> None:
    ex = settings.execution
    if ex.mode != "paper" or ex.leverage_enabled or ex.margin_enabled:
        raise LiveTradingForbidden("execution must be paper-only, no leverage, no margin")
    if settings.risk.allow_averaging_down or settings.risk.allow_martingale:
        raise LiveTradingForbidden("averaging down / martingale are forbidden")


def assert_url_allowed(url: str) -> None:
    """Block any URL that targets live order-execution endpoints."""
    p = urlparse(url)
    target = (p.netloc + p.path).lower()
    host = p.netloc.lower()
    for bad in FORBIDDEN_LIVE_ENDPOINTS:
        bad_host = bad.split("/", 1)[0]
        if "/" in bad:
            if target.startswith(bad):
                raise LiveTradingForbidden(f"live endpoint blocked: {url}")
        elif host == bad_host:
            raise LiveTradingForbidden(f"live endpoint blocked: {url}")


@dataclass
class SafeStateStatus:
    halted: bool
    reason: str | None
    since: str | None


class SafeState:
    def __init__(self, db: Database):
        self.db = db

    def status(self) -> SafeStateStatus:
        s = self.db.get_state(SAFE_STATE_KEY, {"halted": False, "reason": None, "since": None})
        return SafeStateStatus(bool(s.get("halted")), s.get("reason"), s.get("since"))

    def halt(self, reason: str, module: str = "core") -> None:
        cur = self.status()
        if cur.halted and cur.reason == reason:
            return
        self.db.set_state(SAFE_STATE_KEY, {"halted": True, "reason": reason, "since": utcnow()})
        self.db.log_event("safe_state_entered", module, reason, severity="critical")
        log.critical("SAFE STATE entered: %s", reason)

    def resume(self, by: str, note: str = "") -> None:
        self.db.set_state(SAFE_STATE_KEY, {"halted": False, "reason": None, "since": None})
        self.db.log_event("safe_state_cleared", "core", f"resumed by {by}. {note}".strip(),
                          severity="warning")
        log.warning("SAFE STATE cleared by %s", by)

    def trading_allowed(self) -> bool:
        return not self.status().halted
