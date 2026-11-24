"""Data-reliability check: repeated fetches per source, success rate and latency."""
from __future__ import annotations

import time
from statistics import median

from app.data.engine import MarketDataEngine, asset_class_of
from app.data.providers.base import DataFormatError, ProviderError
from app.data.validation import validate_candles


def reliability_check(engine: MarketDataEngine, symbols: list[str], timeframe: str = "5m",
                      rounds: int = 3, pause_s: float = 1.0) -> dict:
    stats: dict[str, dict] = {}
    for rnd in range(rounds):
        for sym in symbols:
            ac = asset_class_of(sym)
            for name in engine.sources_for(ac):
                st = stats.setdefault(name, {"requests": 0, "ok": 0, "unreachable": 0, "invalid": 0,
                                             "latencies_ms": [], "errors": []})
                st["requests"] += 1
                t0 = time.perf_counter()
                try:
                    candles = engine.providers[name].fetch_candles(sym, timeframe, 50)
                    issues, _ = validate_candles(candles, timeframe, ac, engine.cfg)
                except DataFormatError as e:
                    st["invalid"] += 1; st["errors"].append(str(e)[:120]); continue
                except ProviderError as e:
                    st["unreachable"] += 1; st["errors"].append(str(e)[:120]); continue
                finally:
                    st["latencies_ms"].append((time.perf_counter() - t0) * 1000)
                if issues:
                    st["invalid"] += 1; st["errors"].append(issues[0][:120])
                else:
                    st["ok"] += 1
        if rnd < rounds - 1:
            time.sleep(pause_s)
    out = {}
    for name, st in stats.items():
        lat = st.pop("latencies_ms")
        out[name] = {**st, "success_rate": round(st["ok"] / st["requests"], 3) if st["requests"] else None,
                     "median_latency_ms": round(median(lat), 1) if lat else None,
                     "max_latency_ms": round(max(lat), 1) if lat else None,
                     "errors": st["errors"][:5]}
    engine.db.log_event("reliability_check", "data", f"{rounds} rounds x {len(symbols)} symbols",
                        details=out)
    return out
