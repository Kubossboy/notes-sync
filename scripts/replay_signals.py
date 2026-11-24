"""Dry-run replay: evaluate every strategy on each past bar of REAL recent data.

Not a backtest (no fills, no P&L - that is milestone 6). It shows how often each
setup fires on real data and how many survive the cost-aware R:R check.
Indicators are computed once on the full frame and sliced; this is equivalent
to recomputing on each prefix because every indicator is causal (tested in
tests/test_indicators.py::test_no_lookahead_in_compute_all). S/R levels are
recomputed on each prefix. Order-book strategies are skipped (no historic books).

Usage: .venv/bin/python scripts/replay_signals.py [SYMBOL ...]
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_secrets, load_settings  # noqa: E402
from app.data.engine import PROVIDER_CLASSES, asset_class_of  # noqa: E402
from app.data.models import AssetClass  # noqa: E402
from app.scanners.scoring import build_plan, cost_bps_per_side  # noqa: E402
from app.strategies.base import NoSetup, StrategyContext  # noqa: E402
from app.strategies.crypto import CRYPTO_STRATEGIES  # noqa: E402
from app.strategies.stock import STOCK_STRATEGIES  # noqa: E402
from app.technical.indicators import compute_all, support_resistance, to_frame  # noqa: E402


def main(symbols):
    s = load_settings()
    for sym in symbols:
        ac = asset_class_of(sym)
        stock = ac == AssetClass.STOCK
        tf = s.scanner.stock_timeframe if stock else s.scanner.crypto_timeframe
        prov = PROVIDER_CLASSES["yahoo" if stock else "coinbase"]()
        candles = prov.fetch_candles(sym, tf, 1000)
        df_all = to_frame(candles)
        base = df_all[df_all["session"] == "regular"] if stock else df_all
        full = compute_all(base, ac)
        table = STOCK_STRATEGIES if stock else CRYPTO_STRATEGIES
        toggles = s.scanner.stock_strategies if stock else s.scanner.crypto_strategies
        cost = cost_bps_per_side(s, ac)
        fired, passed, reasons = Counter(), Counter(), defaultdict(Counter)
        bars = 0
        for i in range(80, len(full)):
            d = full.iloc[: i + 1]
            ctx = StrategyContext(sym, ac, tf, d, df_all[df_all.index <= d.index[-1]] if stock else None,
                                  support_resistance(d), None, [], None, cost)
            bars += 1
            for name, fn in table.items():
                t = toggles.get(name)
                if t is not None and not t.enabled:
                    continue
                try:
                    setup = fn(ctx, dict(t.params) if t else {})
                except NoSetup:
                    continue
                fired[name] += 1
                plan = build_plan(setup, ctx, s)
                if plan.problems:
                    for pr in plan.problems:
                        key = ("rr<min (target capped by resistance)" if "capped" in pr else
                               "rr<min (costs/structure)" if "net reward/risk" in pr else pr.split(" (")[0])
                        reasons[name][key] += 1
                else:
                    passed[name] += 1
        print(f"\n{sym} {tf}: {bars} bars evaluated ({full.index[80].isoformat()} .. {full.index[-1].isoformat()}), "
              f"cost {cost:.1f} bps/side")
        for name in table:
            if fired[name] or name in toggles:
                print(f"  {name:24} fired={fired[name]:3}  passed_rr={passed[name]:3}  "
                      f"rejections={dict(reasons[name])}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["BTC-USD", "ETH-USD", "SOL-USD", "SPY", "NVDA", "TSLA"])
