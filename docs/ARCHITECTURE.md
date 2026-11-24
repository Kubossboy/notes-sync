# Architecture (Milestones 1-7)

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

## Intelligence (Milestone 4)

```
sources.FEEDS (RSS/Atom, tiered 1-4) ─┐
SEC EDGAR submissions + Form 4 ───────┼─> intelligence.engine.NewsEngine.ingest
X API v2 (only with X_BEARER_TOKEN) ──┘     dedupe (content hash incl. analyzer version)
                                            -> tagging.TickerTagger (cashtag/ticker/name; summary-only mentions ×0.5)
                                            -> catalysts.detect (≈45 rules + SEC 8-K item map, negation -> direction 0)
                                            -> sentiment = 0.6 × catalyst direction + 0.4 × finance lexicon
                                            -> confidence = tier × time quality × tag confidence
                                            -> story clustering (Jaccard ≥ 0.45 within 36 h) -> verification
                                            -> journal: news_events, sentiment_results, insider_transactions, news_ingest_runs
NewsEngine.symbol_context(symbol, now)  -> 24 h half-life-weighted aggregate (one vote per story), catalysts,
                                           verified negative high-impact list, macro / crypto-market backdrop
sentiment.aggregate.combine             -> news 40 / X 15 / price action 25 / volume 10 / macro 10 (available only),
                                           conflicts, social clipping, cap at 0 on verified negative catalyst
scanners.scoring                        -> news component, sentiment component, long veto
strategies.stock.news_momentum          -> fresh positive catalyst + VWAP / new high / RVOL confirmation
```

Failure handling: one source failing = warning (`system_events.news_source_failed`);
health = last ingest ≤ 60 min old and ≥ 50 % of sources ok, otherwise the
news and sentiment components are `None` (signal incomplete); **all** sources
failing = SAFE STATE.

## Risk & paper execution (Milestone 5)

```
PaperTrader.run_once(now)
  1. manage open positions: real 1m bars since entry (incl. forming bar) -> stop / target / time exit,
     mark-to-market. Cannot fetch data for an open position -> SAFE STATE (no new trades), retried next cycle
  2. if trading allowed: scanner (stocks + crypto, live) -> signals with status 'new' and score >= 70
  3. quote (cross-checked consensus + best bid/ask) -> RiskEngine.evaluate  (deterministic rules, all journaled)
  4. approved -> PaperSimulator.open_position (order_requests -> simulated_fills -> positions, one transaction)
     [optional, OFF: AlpacaPaperBroker.submit_bracket mirror for stocks, paper endpoint only]
  5. Ledger.state -> equity_snapshots
kill(reason, flatten) -> SAFE STATE + market-close every open paper position
```

Idempotency: entry `client_order_id = atlas-entry-sig<signal_id>` (UNIQUE), exit
`atlas-exit-pos<position_id>`; partial unique index = one open position per symbol;
closed positions cannot be re-opened; fills immutable; audit tables append-only.

## Backtesting & validation (Milestone 6)

```
history.load(source, symbol, tf, days)     Coinbase 15m / Alpaca IEX 5m / Yahoo 5m (60 d) -> var/history/*.csv.gz
Backtester.prepare -> compute_all (causal)  stocks: regular session only
Backtester.candidates                       bar i: live strategy fn on rows <= i (400-bar window) + build_plan gate
Backtester.simulate                         next-open entry (CostModel.buy_fill), stop/target/EOD/max-hold exits,
                                            size_position() shared with the risk engine, portfolio + loss limits
validation.run_validation                   IS 60 % / OOS 40 %, walk-forward 6 blocks, loose/strict, cost x1.5,
                                            diagnostic R:R>=1 run -> verdict -> backtest_runs / backtest_trades
QuantEngine.assess (scanner._finalize)      failed -> 0 + rejected; unavailable -> None (incomplete);
                                            validated -> 0-100 + expected value (OOS expectancy) in evidence
```

## Forward test & dashboard (post-M6, Milestone 7)

```
QuantEngine.status: news_momentum (not backtestable, configured) -> forward_test (quant 50)
RiskEngine: rule strategy_validated_or_forward_test        PaperTrader: forward_test never mirrored to Alpaca
PaperSimulator.close_position -> forward_test_trades (append-only)
dashboard/data.collect (SQLite mode=ro) -> server.render (HTML) | /api/state (JSON)   127.0.0.1:8765
scripts/run_loop.sh: `trade` every 60 s until var/STOP
```
