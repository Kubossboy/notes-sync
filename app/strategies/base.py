"""Strategy framework: context in, Setup (or a no-setup reason) out.

Strategies are LONG-ONLY (config execution.allow_short=false) and evaluate the
LAST COMPLETE bar only. Entry/stop/target are proposals; the deterministic risk
engine (milestone 5) has the final word.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from datetime import datetime
from typing import Callable

import pandas as pd

from app.data.models import AssetClass


@dataclass
class StrategyContext:
    symbol: str
    asset_class: AssetClass
    timeframe: str
    df: pd.DataFrame                    # complete bars + indicator columns (stocks: regular session)
    df_all: pd.DataFrame | None = None  # stocks: all sessions incl. pre/post (context only)
    levels: list[dict] = field(default_factory=list)   # support/resistance
    book: object | None = None          # BookSnapshot (crypto)
    book_history: list[dict] = field(default_factory=list)
    now: datetime | None = None
    cost_bps_per_side: float = 0.0
    news: dict | None = None            # NewsEngine.symbol_context (None = news engine off/unavailable)
    sentiment: dict | None = None       # app.sentiment.aggregate.combine output

    @cached_property            # df is never mutated after the context is built
    def last(self) -> pd.Series:
        return self.df.iloc[-1]

    @cached_property
    def prev(self) -> pd.Series:
        return self.df.iloc[-2]


@dataclass
class Setup:
    strategy: str
    symbol: str
    direction: str
    entry: float
    stop: float
    target_r: float                     # proposed target as R multiple (before obstacle cap)
    evidence: dict = field(default_factory=dict)


class NoSetup(Exception):
    """Raised (or returned) with a human-readable reason when conditions are not met."""


StrategyFn = Callable[[StrategyContext, dict], Setup]


def need(cond: bool, reason: str) -> None:
    if not cond:
        raise NoSetup(reason)


def val(row: pd.Series, col: str) -> float | None:
    v = row.get(col) if hasattr(row, "get") else None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def req(row: pd.Series, *cols: str) -> list[float]:
    out = []
    for c in cols:
        v = val(row, c)
        if v is None:
            raise NoSetup(f"insufficient history for {c}")
        out.append(v)
    return out
