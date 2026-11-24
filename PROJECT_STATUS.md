# PROJECT STATUS – ATLAS Trading AI

_Last updated: 2026-11-24 (Europe/Prague) · version 0.5.0-m5_

## Current development phase
Milestone 5 – Risk engine and paper execution: **complete**. Next: Milestone 6 – Backtesting and validation.

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

## Remaining tasks
M6 backtesting/validation (quant component) → M7 dashboard → M8 live paper trading.

## Known bugs / limitations
- Signals remain `incomplete` until the quant component (M6) exists: max reachable score 85, so live `trade` cycles execute nothing yet by design. Execution path verified with a labelled synthetic signal on real BTC quotes/bars (temp DB).
- Simulator limits: no partial fills, no queue/latency model, stops evaluated on 1m bars (intra-minute order unknown → stop assumed first), after-hours EOD exits use extended-hours quotes. Crypto costs are material: a 2.5 R gross target nets < 2 R with tight stops, so many crypto setups will be rejected.
- Rule/lexicon NLP works on headlines + feed summaries only (no article bodies): misses nuance, sarcasm and complex negation (negated catalysts get direction 0). Spot checks found and fixed false positives (buyouts, "warp", "warns", Fed enforcement actions); more will exist.
- Yahoo headline RSS includes many listicles/opinion pieces; they carry low weight but add noise. No earnings calendar yet (no free keyless source reachable: Nasdaq API timed out).
- CNBC and GlobeNewswire RSS return 403 to scripts; not used.
- Story clustering is title-similarity based (Jaccard ≥ 0.45); differently worded reports of the same event may stay separate (= unverified).
- Dev database contains news rows from analyzer versions 0-2 (development iterations); only the current version (3) is read.
- Exchange status pages announce future maintenance; such items are stored but excluded from context until their time.
- Strategy parameters (incl. news momentum) are unvalidated hypotheses until M6.
- Earlier limits unchanged: Alpaca IEX partial volume, NYSE calendar 2025-2027 only, order-book imbalance spoofable.

## API integrations (probe 2026-11-24 ~10:10 Europe/Prague): 13/14 ok (Fed RSS returned one transient 404 at ~10:04)
Yahoo, Alpaca market data (IEX), Alpaca paper account (ACTIVE), Binance, Coinbase, Kraken, CoinGecko, SEC EDGAR ×3, Fed RSS, MarketWatch RSS, CoinDesk RSS: ok. X API v2: needs_credentials.
News ingest (real, 2026-11-24 ~09:52): 16/16 sources ok (15 feeds + SEC), 436 items, 172 verified, 142 with a catalyst, 67 Form 4 transactions, 0 items without a publication time.

## Required credentials
- Alpaca: env vars (present). SEC contact e-mail: `.env` (set). X API bearer token: missing (paid tier for search). Finnhub/NewsAPI: not used (would need sign-ups).

## Test results
Offline: 240 passed. Live (real APIs): 32 passed (incl. real BTC paper cycle: risk-approved entry at real ask, management on real 1m bars, kill-switch flatten; Alpaca paper read-only eligibility). 0 failed.
Live tests at the 04:00 ET pre-market open exposed a Yahoo parser gap (off-grid snapshot bar) → fixed (D63).

## Backtesting results
None yet (Milestone 6).

## Paper trading performance
No trades yet (no complete signals until M6). Simulated equity: $1,000.00, daily loss limit $10.00, risk per trade $2.50.

## Current system configuration
Paper only · internal_sim · $1,000 · 0.25 %/trade ($2.50) · daily loss 1 % ($10) · max 2 positions · R:R ≥ 2 (net of costs) ·
leverage/margin off · min score 70 · weights 25/20/20/10/15/10 · stocks 5m / crypto 15m · crypto fee 10 bps + 10 bps slippage per side
(pending Kubiqq's confirmation) · stock 0 fee + 5 bps slippage · target 2.5 R capped by resistance ·
execution: internal_sim, Alpaca paper orders OFF, fractional stocks, min notional $5, stock flatten 5 min before close, crypto max hold 24 h ·
news: refresh 15 min, health ≤ 60 min & ≥ 50 % sources, window 24 h, half-life 6 h, catalyst freshness 16 h.

## Next development priorities (Milestone 6)
Historical backtesting with the same cost/fill model, out-of-sample and walk-forward validation, strategy comparison,
drawdown and parameter-sensitivity analysis; derive the quant score component so signals can become complete.
