# PROJECT STATUS – ATLAS Trading AI

_Last updated: 2026-11-24 (Europe/Prague) · version 0.7.4-m7 (dashboard v2 + public read-only snapshot + Alpaca PAPER mirror ON)_

## Current development phase
Milestone 7 – Dashboard: **complete** (plus the news-driven forward test). Next: Milestone 8 – continuous paper trading.

## Completed modules
- M1 foundation: config (strict, paper-only), logging + redaction, SQLite journal (append-only), safety/SAFE STATE, probes, CLI.
- M2 market data: Yahoo / Coinbase / Kraken / Binance (+ Alpaca IEX) providers, validation, freshness labels, cross-checks, cache, reliability.
- M3: indicators, L2 order books, 7 stock + 7 crypto strategies, cost-aware scoring, scanners, signal journal.
- M4 intelligence:
  - News intake from 15 free RSS/Atom feeds (tiers 1-3) + SEC EDGAR (8-K items, 10-Q/10-K/S-1/S-3/424B/13D/G, Form 4 insider transactions)
  - Ticker tagging (cashtag / ticker / company & coin names, ambiguity rules, summary-only mentions down-weighted), MACRO and CRYPTO_MARKET tags
  - Catalyst detection (~45 rules + 8-K item map; positive/negative, impact, unexpected flag, negation handling)
  - Lexicon + rule sentiment per item; source / publication time / confidence on every event
  - Dedup (content hash), story clustering, verification (primary source or ≥ 2 independent sources)
  - Sentiment engine combining news, X (if token), price action + benchmark trend, volume anomaly, macro; conflict list; social clipping; negative-catalyst cap
  - Scoring hookup: news (20 %) and sentiment (10 %) components; verified high-impact negative catalyst vetoes longs
  - News-driven momentum strategy enabled
  - X API v2 client gated on `X_BEARER_TOKEN` (absent → needs_credentials, never claims to monitor X)
  - CLI: `news ingest | show SYMBOL | latest | macro | health`, `scan --no-news`; scanner auto-ingests when news > 15 min old

- M5 risk & execution:
  - Deterministic risk engine (~27 rules, every evaluation + rule journaled; failure → SAFE STATE)
  - Sizing from 0.25 % of current equity incl. fees/spread/slippage; 50 % position cap; no leverage; daily loss 1 %; 3-loss stop; 60 min symbol cooldown; no chasing; stock session/close rules
  - Internal paper simulator: market entries at ask + slippage, stop-market (gap-aware), take-profit limit, EOD flatten (stocks), 24 h max hold (crypto), mark-to-market, MFE/MAE, R multiple
  - Order tracking (order_requests with idempotent client ids, immutable simulated_fills), positions (one open per symbol at DB level), equity snapshots, own $1,000 ledger with day-start equity
  - Kill switch (`kill --flatten`), CLI: trade, portfolio, positions, orders, risk
  - Alpaca paper adapter: bracket-order mirroring implemented, OFF by default; read-only eligibility checked

- M6 backtesting & validation:
  - Free historical bars cached in `var/history`: Coinbase 15m (2 y), Alpaca IEX 5m (2 y), Yahoo 5m (60 d robustness)
  - Bar-by-bar replay of the live strategies + plan gate + cost/fill model + shared `size_position()` + portfolio/loss rules
  - IS/OOS (60/40), 5-fold walk-forward, loose/strict sensitivity, ×1.5 cost stress, regime / per-symbol breakdown, diagnostic R:R≥1 run
  - Verdicts journaled in append-only `backtest_runs` / `backtest_trades`; QuantEngine feeds the quant score component
  - CLI: `backtest`, `validation`; results in docs/BACKTEST_RESULTS.md

- News-driven forward test (D75-D79): `forward_test` status for news_momentum (internal ledger only, never Alpaca), score weights news 40 / sentiment 15 / quant 15 / liquidity 10 / technical 10 / momentum 10, verified catalysts required, session-aware freshness, separate `forward_test_trades` log, `forwardtest` CLI.
- M7 dashboard: local read-only web dashboard (`dashboard`), all spec sections + forward test + validation + risk; SIMULATED vs REAL labels.

- Shadow forward test (D91): every strategy (also failed / unavailable) trades a separate simulated ledger; promotion after >= 30 trades with PF >= 1.3, expectancy > 0, DD <= $50; demotion on rolling PF < 1.0; dashboard section "Strategie (stínový test)".

## Remaining tasks
M8: run the paper loop continuously (`scripts/run_loop.sh`), monitor errors, collect ≥ 30 forward-test trades (≥ 100 for spec §14 acceptance).

## Known bugs / limitations
- **No strategy is validated** (stocks: all failed OOS; crypto: insufficient trades after the net R:R gate, negative in the diagnostic). So live `trade` cycles execute nothing, by design. Execution path verified with a labelled synthetic signal (M5).
- Backtest limits: survivorship bias, IEX partial volume, assumed spreads, exits on 5m/15m bars (stop-first), composite score (news/sentiment) not reproducible historically; news_momentum and order-book strategies are not backtestable (no free archives).
- Simulator limits: no partial fills, no queue/latency model, stops evaluated on 1m bars (intra-minute order unknown → stop assumed first), after-hours EOD exits use extended-hours quotes. Crypto costs are material: a 2.5 R gross target nets < 2 R with tight stops, so many crypto setups will be rejected.
- Rule/lexicon NLP works on headlines + feed summaries only (no article bodies): misses nuance, sarcasm and complex negation (negated catalysts get direction 0). Spot checks found and fixed false positives (buyouts, "warp", "warns", Fed enforcement actions); more will exist.
- Yahoo headline RSS includes many listicles/opinion pieces; they carry low weight but add noise. No earnings calendar yet (no free keyless source reachable: Nasdaq API timed out).
- CNBC and GlobeNewswire RSS return 403 to scripts; not used.
- Story clustering is title-similarity based (Jaccard ≥ 0.45); differently worded reports of the same event may stay separate (= unverified).
- Dev database contains news rows from analyzer versions 0-2 (development iterations); only the current version (3) is read.
- Exchange status pages announce future maintenance; such items are stored but excluded from context until their time.
- Earlier limits unchanged: Alpaca IEX partial volume, NYSE calendar 2024-2027 only, order-book imbalance spoofable.

## API integrations (probe 2026-11-24 ~10:56 Europe/Prague): 12/14 ok (Fed RSS HTTP 404 again, recurring; X needs credentials)
Yahoo, Alpaca market data (IEX), Alpaca paper account (ACTIVE), Binance, Coinbase, Kraken, CoinGecko, SEC EDGAR ×3, Fed RSS, MarketWatch RSS, CoinDesk RSS: ok. X API v2: needs_credentials.
News ingest (real, 2026-11-24 ~09:52): 16/16 sources ok (15 feeds + SEC), 436 items, 172 verified, 142 with a catalyst, 67 Form 4 transactions, 0 items without a publication time.

## Required credentials
- Alpaca: env vars (present). SEC contact e-mail: `.env` (set). X API bearer token: missing (paid tier for search). Finnhub/NewsAPI: not used (would need sign-ups).

## Test results
Offline: 270 passed. Live (real APIs): 35 passed (M6: real Coinbase / Alpaca IEX / Yahoo history + backtest on it) (incl. real BTC paper cycle: risk-approved entry at real ask, management on real 1m bars, kill-switch flatten; Alpaca paper read-only eligibility). 0 failed.
Live tests at the 04:00 ET pre-market open exposed a Yahoo parser gap (off-grid snapshot bar) → fixed (D63).

## Backtesting results
See docs/BACKTEST_RESULTS.md.
- Stocks: 6/6 strategies failed, with OOS PF 0.57–1.01.
- Crypto: 5/5 have insufficient data under the live gate, and the diagnostic PF is 0.55–0.87.
- 3 strategies are not backtestable.
- No edge after costs, so nothing is tradable.

## Paper trading performance
No trades (no validated strategy → no complete signals). Simulated equity: $1,000.00, daily loss limit $10.00, risk per trade $2.50.

## Current system configuration
Paper only · internal_sim · $1,000 · 0.25 %/trade ($2.50) · daily loss 1 % ($10) · max 2 positions · R:R ≥ 2 (net of costs) ·
leverage/margin off · min score 70 · weights 25/20/20/10/15/10 · stocks 5m / crypto 15m · crypto fee 10 bps + 10 bps slippage per side
(pending Kubiqq's confirmation) · stock 0 fee + 5 bps slippage · target 2.5 R capped by resistance ·
execution: internal_sim, Alpaca paper orders OFF, fractional stocks, min notional $5, stock flatten 5 min before close, crypto max hold 24 h ·
news: refresh 15 min, health ≤ 60 min & ≥ 50 % sources, window 24 h, half-life 6 h, catalyst freshness 16 h.

## Next development priorities (Milestone 8)
Start the continuous paper loop (Kubiqq's call), watch the dashboard, judge the news forward test after ≥ 30 trades.
