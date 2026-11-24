"""Static, read-only dashboard snapshot for publishing (Cloudflare Pages).

Writes   <out>/index.html            decoy "nothing here" page (noindex)
         <out>/<slug>/index.html     the dashboard (static mode: refetches top.html + state.json every 60 s)
         <out>/<slug>/top.html       cards + position charts + balance chart
         <out>/<slug>/state.json     scrubbed state
         <out>/_headers, robots.txt  noindex / no-store / no-referrer for every path

Everything is scrubbed before rendering: filesystem paths, e-mail addresses, URLs with query strings,
long token-like strings and the literal values of any credential env vars are replaced by "[redacted]".
The DB is opened read-only (same collector as the local dashboard); nothing is written to it.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.dashboard.data import collect
from app.dashboard.server import render, render_top

_PATH = re.compile(r"(?:[A-Za-z]:\\|~/|/(?:workspace|home|cursor|tmp|var|etc|usr|root|opt|mnt|proc|srv)\b)[^\s'\"<>),;]*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_QURL = re.compile(r"https?://[^\s'\"<>]*\?[^\s'\"<>]*")
_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b")
_SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|BEARER|EMAIL)", re.I)
RED = "[redacted]"


def _secret_values() -> list[str]:
    """Values of credential-like env vars and of every entry in the project .env (never printed)."""
    vals = {v for k, v in os.environ.items() if _SECRET_ENV.search(k) and v and len(v) >= 6}
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if len(v) >= 6:
                    vals.add(v)
    return sorted(vals, key=len, reverse=True)


def scrub(obj, secrets_=None):
    """Recursively redact anything that could identify the box or leak a credential."""
    secrets_ = _secret_values() if secrets_ is None else secrets_
    if isinstance(obj, dict):
        return {k: scrub(v, secrets_) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(scrub(v, secrets_) for v in obj) if isinstance(obj, list) else tuple(scrub(v, secrets_) for v in obj)
    if isinstance(obj, str):
        s = obj
        for v in secrets_:
            s = s.replace(v, RED)
        s = _QURL.sub(RED, s)
        s = _EMAIL.sub(RED, s)
        s = _PATH.sub(RED, s)
        return _TOKEN.sub(RED, s)
    return obj


def publish_config(settings: Settings) -> dict:
    """Random, unguessable project name + path slug, created once and kept in var/ (git-ignored)."""
    p = Path(settings.db_path).resolve().parent / "publish.json"
    if p.exists():
        return json.loads(p.read_text())
    cfg = {"project": "atlas-" + secrets.token_hex(5), "slug": secrets.token_urlsafe(18)}
    p.write_text(json.dumps(cfg, indent=2))
    return cfg


HEADERS = """/*
  X-Robots-Tag: noindex, nofollow, noarchive
  Referrer-Policy: no-referrer
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Cache-Control: no-store
"""
DECOY = ('<!doctype html><html><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">'
         '<title>404</title></head><body>Not found</body></html>')


def export(settings: Settings, out: Path, slug: str, db_path=None, live_prices: bool = True) -> Path:
    d = collect(settings, db_path, live_prices=live_prices)
    d = scrub(json.loads(json.dumps(d, default=str)))  # JSON round-trip: only plain data survives
    d2 = _rehydrate(json.loads(json.dumps(d)))          # charts need datetimes back
    site = out / slug
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(render(d2, static=True))
    (site / "top.html").write_text(render_top(d2))
    public = {k: d[k] for k in ("generated_ts", "overview", "forward_test", "equity_curve", "start_equity") if k in d}
    public["positions"] = [{k: v for k, v in p.items() if k != "candles"} for p in d.get("portfolio", [])]
    (site / "state.json").write_text(json.dumps(public, default=str, indent=1))
    (out / "index.html").write_text(DECOY)
    (out / "_headers").write_text(HEADERS)
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    return site


def _rehydrate(d: dict) -> dict:
    def dt(x):
        return datetime.fromisoformat(x) if isinstance(x, str) else x
    for p in d.get("portfolio", []):
        p["candles"] = [(dt(c[0]), *c[1:]) for c in p.get("candles", [])]
    d["equity_curve"] = [(dt(t), v) for t, v in d.get("equity_curve", [])]
    return d
