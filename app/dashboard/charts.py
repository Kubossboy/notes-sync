"""Dashboard charts: real candles for open positions + the simulated equity curve, as inline SVG.

Candles come straight from the public providers (Coinbase 15m for crypto, Yahoo 5m for stocks; read-only HTTP,
nothing written to the DB), cached 45 s. If the fetch fails, it falls back to candles already journaled
in market_observations. If there is no data, the chart says so. Nothing is ever invented.
"""
from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from app.data.models import AssetClass

PRAGUE = ZoneInfo("Europe/Prague")
_CACHE: dict = {}
TTL = 45.0


def fetch_candles(symbol: str, ac: str, n: int = 64) -> tuple[list, str, str]:
    """Returns ([(t, o, h, l, c)], timeframe, source). Live public data, cached TTL seconds."""
    from app.data.providers.crypto import CoinbaseProvider
    from app.data.providers.yahoo import YahooProvider
    key = (symbol, ac)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    tf, prov = ("15m", CoinbaseProvider) if ac == AssetClass.CRYPTO.value else ("5m", YahooProvider)
    try:
        cs = prov(session=requests.Session(), timeout=6).fetch_candles(symbol, tf, n)
        out = ([(c.open_time, c.open, c.high, c.low, c.close) for c in cs[-n:]], tf, prov.__name__.replace("Provider", "").lower())
    except Exception:
        out = ([], tf, "unavailable")
    _CACHE[key] = (time.monotonic(), out)
    return out


def db_candles(db, symbol: str, tf: str, n: int = 64) -> tuple[list, str, str]:
    rows = db.conn.execute(
        "SELECT source_ts, open, high, low, close, source FROM market_observations WHERE symbol=? AND kind='candle' "
        "AND timeframe=? AND open IS NOT NULL ORDER BY source_ts DESC LIMIT ?", (symbol, tf, n * 6)).fetchall()
    if not rows:
        return [], tf, "none"
    src, seen, out = rows[0][5], set(), []
    for r in rows:
        if r[5] == src and r[0] not in seen:
            seen.add(r[0])
            out.append((datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4]))
    return list(reversed(out[:n])), tf, f"{src} (journal)"


def _px(p: float) -> str:
    """Readable price label, never scientific notation."""
    return f"{p:,.0f}" if p >= 1000 else f"{p:,.2f}" if p >= 1 else f"{p:.6f}"


def position_chart_svg(candles: list, entry: float, stop: float, target: float, mark: float | None,
                       w: int = 640, h: int = 260) -> str:
    if not candles:
        return '<div class="empty">No price data available right now - not drawing anything rather than guessing.</div>'
    pad_l, pad_r, pad_t, pad_b = 8, 96, 10, 22
    lo = min(min(c[3] for c in candles), stop)
    hi = max(max(c[2] for c in candles), target)
    span = (hi - lo) or 1.0
    lo, hi = lo - span * 0.04, hi + span * 0.04
    y = lambda p: pad_t + (hi - p) / (hi - lo) * (h - pad_t - pad_b)  # noqa: E731
    n = len(candles)
    cw = (w - pad_l - pad_r) / n
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="price chart">']
    for i, (t, o, hh, ll, c) in enumerate(candles):
        x = pad_l + i * cw + cw / 2
        col = "#34d399" if c >= o else "#f87171"
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y(hh):.1f}" y2="{y(ll):.1f}" stroke="{col}" stroke-width="1"/>')
        top, bot = y(max(o, c)), y(min(o, c))
        parts.append(f'<rect x="{x - cw * 0.35:.1f}" y="{top:.1f}" width="{cw * 0.7:.1f}" height="{max(bot - top, 1):.1f}" fill="{col}"/>')
    for label, p, col in (("target", target, "#34d399"), ("entry", entry, "#60a5fa"), ("stop", stop, "#f87171")):
        parts.append(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{y(p):.1f}" y2="{y(p):.1f}" stroke="{col}" '
                     f'stroke-dasharray="5 4" stroke-width="1.4"/>'
                     f'<text x="{w - pad_r + 4}" y="{y(p) + 4:.1f}" fill="{col}" font-size="11">{label} {_px(p)}</text>')
    if mark:
        parts.append(f'<circle cx="{w - pad_r}" cy="{y(mark):.1f}" r="3.5" fill="#fbbf24"/>')
    for i in (0, n // 2, n - 1):
        t = candles[i][0].astimezone(PRAGUE).strftime("%H:%M")
        parts.append(f'<text x="{pad_l + i * cw:.1f}" y="{h - 6}" fill="#718396" font-size="10">{t}</text>')
    parts.append("</svg>")
    return "".join(parts)


def equity_svg(points: list[tuple[datetime, float]], start: float, w: int = 900, h: int = 220) -> str:
    if len(points) < 2:
        return (f'<div class="empty">Not enough equity history yet ({len(points)} snapshot(s)). Balance is '
                f'${points[-1][1] if points else start:,.2f} (start ${start:,.2f}); the curve appears after the first cycles/trades.</div>')
    pad_l, pad_r, pad_t, pad_b = 56, 12, 12, 22
    vals = [v for _, v in points] + [start]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1.0:                                  # flat: show a +/- $1 band instead of exaggerating noise
        lo, hi = (lo + hi) / 2 - 1, (lo + hi) / 2 + 1
    t0, t1 = points[0][0].timestamp(), points[-1][0].timestamp()
    x = lambda t: pad_l + (t.timestamp() - t0) / ((t1 - t0) or 1) * (w - pad_l - pad_r)  # noqa: E731
    y = lambda v: pad_t + (hi - v) / (hi - lo) * (h - pad_t - pad_b)  # noqa: E731
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(t):.1f},{y(v):.1f}" for i, (t, v) in enumerate(points))
    col = "#34d399" if points[-1][1] >= start else "#f87171"
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="equity curve">',
           f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{y(start):.1f}" y2="{y(start):.1f}" stroke="#4b5563" stroke-dasharray="4 4"/>',
           (f'<text x="4" y="{y(start) + 4:.1f}" fill="#9ca3af" font-size="10">${start:,.0f}</text>'
            if pad_t + 20 < y(start) < h - pad_b - 14 else ''),
           f'<text x="4" y="{pad_t + 8}" fill="#9ca3af" font-size="10">${hi:,.2f}</text>',
           f'<text x="4" y="{h - pad_b}" fill="#9ca3af" font-size="10">${lo:,.2f}</text>',
           f'<path d="{path}" fill="none" stroke="{col}" stroke-width="2"/>']
    for t in (points[0][0], points[-1][0]):
        out.append(f'<text x="{min(x(t), w - 90):.1f}" y="{h - 6}" fill="#718396" font-size="10">'
                   f'{html.escape(t.astimezone(PRAGUE).strftime("%d.%m %H:%M"))}</text>')
    out.append("</svg>")
    return "".join(out)


def equity_points(db, current: float, now: datetime | None = None) -> list[tuple[datetime, float]]:
    rows = db.conn.execute("SELECT ts, equity_usd FROM equity_snapshots ORDER BY ts").fetchall()
    pts = [(datetime.fromisoformat(r[0]), float(r[1])) for r in rows]
    if pts:
        pts.append((now or datetime.now(timezone.utc), current))
    return pts
