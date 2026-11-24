# Architecture (Milestones 1-3)

```
CLI (app/main.py)
  └─ core.bootstrap ─┬─ config.load_settings / load_secrets   (validated, frozen)
                     ├─ core.logging_setup                    (JSON lines + redaction)
                     ├─ core.safety.assert_paper_only         (refuses unsafe config)
                     ├─ database.Database.migrate + integrity (SQLite, append-only audit)
                     └─ core.safety.SafeState                 (persistent halt flag)
integrations.registry ── probe_all → integration_health table
```

Data flow planned for later milestones:
market data → normalisation/validation → indicators/strategies → signals →
scoring (0-100, configurable weights) → deterministic risk engine → paper
execution → positions/equity → analytics/dashboard. The risk engine is pure
deterministic code; any AI/LLM component may only *propose* signals.

Safety layers (defence in depth):
1. Config types (`Literal["paper"]`, `Literal[False]` for leverage/margin).
2. `assert_url_allowed` blocks live order endpoints (Alpaca live, Coinbase
   brokerage orders, Binance order, Kraken private).
3. DB `CHECK (execution_mode = 'paper')`, unique `client_order_id`,
   mandatory `stop_loss`/`take_profit` on positions.
4. SAFE STATE stops new simulated trades on data/DB/integration failures.

## Market data engine (Milestone 2)

```
providers (yahoo | coinbase | kraken | binance)    raw JSON -> Candle / Quote (UTC, normalized)
   └─ BaseProvider._get_json: URL guard, timeout, retries, rate limit
validation.validate_candles   issues (reject + SAFE STATE) / warnings (gaps, spikes, nulls)
validation.label_freshness    live | delayed | cached | unavailable, stale, market_open
crosscheck.cross_check        median consensus, outliers, divergent
engine.MarketDataEngine       priority + fallback, cache, persistence, safe-state policy
   tables: market_observations (kind=candle|quote), crosscheck_results (append-only)
```

## Scanners & strategies (Milestone 3)

```
MarketDataEngine.get_candles (live, valid)        crypto: + L2 order book snapshot
  -> technical.indicators.compute_all (complete bars only, causal)
  -> support_resistance (confirmed pivots only)
  -> strategies.stock / strategies.crypto  (Setup or NoSetup(reason))
  -> scanners.scoring.build_plan (stop sanity, 2.5R target capped by resistance, net R:R after costs)
  -> component scores (technical, momentum/volume, liquidity; news/sentiment/quant pending)
  -> journal: signals, signal_scores, strategy_decisions, scan_runs, orderbook_snapshots
```
