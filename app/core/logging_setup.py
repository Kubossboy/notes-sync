"""Structured logging: JSON lines to logs/atlas.jsonl + human-readable console.

A RedactingFilter masks every configured secret value and common token
patterns before any handler writes the record.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_TOKEN_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/%=]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|secret|token|password)\s*[=:]\s*)[^\s,;&\"']+"),
]
REDACTED = "***REDACTED***"


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 4]

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, REDACTED)
        for pat in _TOKEN_PATTERNS:
            text = pat.sub(lambda m: m.group(1) + REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        record.msg = self.redact(msg)
        record.args = None
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            record.ctx = json.loads(self.redact(json.dumps(ctx, default=str)))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if ctx:
            payload["ctx"] = ctx
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(log_dir: Path, secrets: list[str] | None = None,
                  level: int = logging.INFO, console: bool = True) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("atlas")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.propagate = False
    redactor = RedactingFilter(secrets)

    fh = RotatingFileHandler(log_dir / "atlas.jsonl", maxBytes=5_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    fh.addFilter(redactor)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        ch.addFilter(redactor)
        root.addHandler(ch)
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"atlas.{name}")
