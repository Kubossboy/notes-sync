"""SENTIMENT ENGINE: combines news sentiment, (optional) X sentiment, price-action
/ market-structure sentiment, volume behaviour and macro context into one
composite in [-1, 1], and lists conflicts between them.

Hard rules (spec):
* social (X) sentiment can never override negative fundamentals or a bearish
  structure - it is clipped to <= 0 in that case;
* a verified high-impact negative catalyst caps the composite at 0;
* conflicts (e.g. positive news vs falling price) are reported, not hidden.
"""
from __future__ import annotations

import math

import pandas as pd

WEIGHTS = {"news": 0.40, "social_x": 0.15, "price_action": 0.25, "volume": 0.10, "macro": 0.10}
TREND_SCORE = {"up": 0.6, "down": -0.6, "range": 0.0}


def _num(x) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def price_action_sentiment(df: pd.DataFrame, market_trend: str | None) -> float | None:
    if df is None or df.empty:
        return None
    last = df.iloc[-1]
    parts = []
    tr = last.get("trend")
    if isinstance(tr, str) and tr in TREND_SCORE:
        parts.append(TREND_SCORE[tr])
    c, vw = _num(last.get("close")), _num(last.get("vwap"))
    if c is not None and vw:
        parts.append(0.4 if c > vw else -0.4)
    roc, ap = _num(last.get("roc10")), _num(last.get("atr_pct"))
    if roc is not None and ap:
        parts.append(max(-1.0, min(1.0, roc / (3 * ap))))
    if market_trend in TREND_SCORE:
        parts.append(TREND_SCORE[market_trend])
    return round(sum(parts) / len(parts), 4) if parts else None


def volume_sentiment(df: pd.DataFrame) -> float | None:
    if df is None or df.empty:
        return None
    last = df.iloc[-1]
    rv = _num(last.get("rvol_tod"))
    rv = rv if rv is not None else _num(last.get("rvol20"))
    if rv is None:
        return None
    if rv < 1.5:
        return 0.0
    sign = 1.0 if float(last["close"]) >= float(last["open"]) else -1.0
    return round(sign * min(1.0, (rv - 1) / 2), 4)


def combine(news_ctx: dict | None, df: pd.DataFrame | None, market_trend: str | None = None,
            x_score: float | None = None) -> dict:
    news_ok = bool(news_ctx and news_ctx["health"]["healthy"])
    comp = {"news": (news_ctx["aggregate"] if news_ok and news_ctx["aggregate"] is not None else (0.0 if news_ok else None)),
            "social_x": x_score,
            "price_action": price_action_sentiment(df, market_trend),
            "volume": volume_sentiment(df),
            "macro": news_ctx.get("macro_aggregate") if news_ok else None}
    conflicts = []
    neg_fund = bool(news_ok and news_ctx["negative_high_verified"])
    bearish = (comp["price_action"] or 0) < -0.2
    if comp["social_x"] is not None and comp["social_x"] > 0 and (neg_fund or bearish):
        conflicts.append("positive social sentiment vs negative fundamentals/structure: social clipped to 0")
        comp["social_x"] = 0.0
    n, p, v = comp["news"], comp["price_action"], comp["volume"]
    if n is not None and p is not None:
        if n > 0.2 and p < -0.2:
            conflicts.append("positive news but bearish price action (market not confirming)")
        if n < -0.2 and p > 0.2:
            conflicts.append("negative news but bullish price action")
    if n is not None and v is not None and n < -0.2 and v > 0.3:
        conflicts.append("negative news but strong buying volume")
    if comp["macro"] is not None and n is not None and comp["macro"] < -0.3 and n > 0.3:
        conflicts.append("positive symbol news against negative macro backdrop")

    avail = {k: w for k, w in WEIGHTS.items() if comp[k] is not None}
    total_w = sum(avail.values())
    composite = sum(comp[k] * w for k, w in avail.items()) / total_w if total_w else None
    capped = False
    if composite is not None and neg_fund and composite > 0:
        composite, capped = 0.0, True
        conflicts.append("verified high-impact negative catalyst: composite capped at 0")
    return {"composite": None if composite is None else round(composite, 4), "components": comp,
            "available_weight": round(total_w, 3), "conflicts": conflicts, "capped": capped,
            "news_available": news_ok,
            "x_status": (news_ctx or {}).get("x_status", "needs_credentials: X_BEARER_TOKEN not set")}


def sentiment_component(sent: dict | None) -> float | None:
    """0-100 score; unavailable (None) when the news engine is not healthy -
    price/volume alone are already scored by the technical/momentum components."""
    if not sent or not sent["news_available"] or sent["composite"] is None:
        return None
    return round((sent["composite"] + 1) * 50, 2)
