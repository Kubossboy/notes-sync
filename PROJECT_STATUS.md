# PROJECT STATUS – ATLAS Trading AI

_Last updated: 2026-11-24 (Europe/Prague) · version 0.4.0-m4_

## Current development phase
Milestone 4 – Intelligence: **complete**. Next: Milestone 5 – Risk engine and paper execution.

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

## Remaining tasks
M5 risk engine + paper execution → M6 backtesting/validation (quant component) → M7 dashboard → M8 live paper trading.

## Known bugs / limitations
- Signals remain `incomplete` until the quant component (M6) exists: max reachable score 85; nothing is tradable yet by design.
- Rule/lexicon NLP works on headlines + feed summaries only (no article bodies): misses nuance, sarcasm and complex negation (negated catalysts get direction 0). Spot checks found and fixed false positives (buyouts, "warp", "warns", Fed enforcement actions); more will exist.
- Yahoo headline RSS includes many listicles/opinion pieces; they carry low weight but add noise. No earnings calendar yet (no free keyless source reachable: Nasdaq API timed out).
- CNBC and GlobeNewswire RSS return 403 to scripts; not used.
- Story clustering is title-similarity based (Jaccard ≥ 0.45); differently worded reports of the same event may stay separate (= unverified).
- Dev database contains news rows from analyzer versions 0-2 (development iterations); only the current version (3) is read.
- Exchange status pages announce future maintenance; such items are stored but excluded from context until their time.
- Strategy parameters (incl. news momentum) are unvalidated hypotheses until M6.
- Earlier limits unchanged: Alpaca IEX partial volume, NYSE calendar 2025-2027 only, order-book imbalance spoofable.

## API integrations (probe 2026-11-24 ~09:51 Europe/Prague): 13/14 ok
Yahoo, Alpaca market data (IEX), Alpaca paper account (ACTIVE), Binance, Coinbase, Kraken, CoinGecko, SEC EDGAR ×3, Fed RSS, MarketWatch RSS, CoinDesk RSS: ok. X API v2: needs_credentials.
News ingest (real, 2026-11-24 ~09:52): 16/16 sources ok (15 feeds + SEC), 436 items, 172 verified, 142 with a catalyst, 67 Form 4 transactions, 0 items without a publication time.

## Required credentials
- Alpaca: env vars (present). SEC contact e-mail: `.env` (set). X API bearer token: missing (paid tier for search). Finnhub/NewsAPI: not used (would need sign-ups).

## Test results
Offline: 204 passed. Live (real APIs): 30 passed (incl. real feed parsing, SEC submissions + Form 4, full ingest, live crypto scan with news). 0 failed.

## Backtesting results
None yet (Milestone 6).

## Paper trading performance
No trades (execution arrives in M5). Simulated equity: $1,000.00.

## Current system configuration
Paper only · internal_sim · $1,000 · 0.25 %/trade ($2.50) · daily loss 1 % ($10) · max 2 positions · R:R ≥ 2 (net of costs) ·
leverage/margin off · min score 70 · weights 25/20/20/10/15/10 · stocks 5m / crypto 15m · crypto fee 10 bps + 10 bps slippage per side
(pending Kubiqq's confirmation) · stock 0 fee + 5 bps slippage · target 2.5 R capped by resistance ·
news: refresh 15 min, health ≤ 60 min & ≥ 50 % sources, window 24 h, half-life 6 h, catalyst freshness 16 h.

## Next development priorities (Milestone 5)
Deterministic risk engine (position sizing from 0.25 % risk, daily loss limit, max positions, exposure, cooldowns, kill switch),
internal paper execution simulator with fees/slippage, order tracking (order_requests/simulated_fills), portfolio & equity journal.
