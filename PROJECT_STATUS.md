# PROJECT STATUS – ATLAS Trading AI

_Last updated: 2026-11-24 (Europe/Prague)_

## Current development phase
Milestone 2 – Market data: **complete**. Next: Milestone 3 – Scanners and strategies.

## Environment
Debian 13 x86_64, 8 vCPU, 15.6 GB RAM (shared), ~100 GB free, Python 3.13.5, uv, git.
`/workspace` persistent; outbound HTTPS works.

## Completed modules
- M1: project structure, config (strict, paper-only), logging (JSON + redaction),
  SQLite (migrations, append-only audit), safety (live-endpoint guard, SAFE STATE),
  integration probes, CLI.
- M2: market data engine
  - Providers: Yahoo (US stocks incl. pre/post), Coinbase, Kraken, Binance (crypto); timeframes 1m/5m/15m/1h/1d
  - Normalized Candle/Quote model, canonical symbols
  - Validation (OHLC, prices, volume, timestamps, grid alignment, duplicates, future bars, gaps, spikes, nulls)
  - Freshness labels live/delayed/cached/unavailable + stale + market_open; forming-bar flag; `tradable`
  - NYSE session calendar 2025-2027 (holidays, early closes, DST via zoneinfo)
  - Cross-source price checks (candles + bid/ask quotes), consensus, outliers
  - Persistence (market_observations, crosscheck_results), cache fallback
  - SAFE STATE on invalid data / divergence / total outage
  - Reliability check (success rate, latency per source)
  - CLI: candles, quote, collect, reliability

## Remaining tasks
M3 scanners/strategies → M4 news/sentiment → M5 risk engine + paper execution →
M6 backtesting → M7 dashboard → M8 live paper trading.

## Known bugs / limitations
- Stocks have a single free keyless source (Yahoo, unofficial). Stock prices cannot be cross-verified (`single_source`); Stooq returned 404.
- Yahoo chart has no bid/ask -> stock spreads unknown until a second source/credentials (e.g. Alpaca paper data).
- Live labelling during the US regular session is covered by unit tests only; the live tests ran at ~03:00-03:20 ET (market closed), so stocks were correctly labelled `delayed`.
- Yahoo leaves an off-grid "last tick" bar after post-market close; it stays flagged FORMING (excluded from use).
- Binance is USDT-quoted (≈11-14 bps above USD sources today); within tolerance, documented.
- NYSE calendar must be extended for 2028+ (raises CalendarUnknown, fail closed).
- 5 SAFE STATE entries in the dev DB on 2026-11-24 came from a column-name bug found during development (fixed, resumed, journaled).

## API integrations (live, 2026-11-24 ~09:20 Europe/Prague)
| Source | Status | Reliability check (3 rounds) |
|---|---|---|
| Yahoo chart (stocks) | ok | 24/24, median 499 ms |
| Coinbase Exchange public | ok | 9/9, median 16 ms |
| Kraken public | ok | 9/9, median 181 ms |
| Binance public mirror | ok | 9/9, median 436 ms |
| SEC EDGAR, Fed RSS, MarketWatch/CoinDesk RSS, CoinGecko | ok (probe only; used from M4) | – |
| Alpaca paper | needs_credentials | – |
| X API v2 | needs_credentials | – |

Live cross-check: BTC/ETH/SOL candles and quotes `agree` (Coinbase vs Kraken ≤ 5 bps, Binance USDT +11-14 bps).

## Required credentials (all optional)
`ALPACA_API_KEY_ID`/`ALPACA_API_SECRET_KEY` (free paper account; would add a second stock source with bid/ask),
`X_BEARER_TOKEN`, `ATLAS_CONTACT_EMAIL` (SEC User-Agent).

## Test results
`pytest -m "not live"`: 104 passed. `pytest -m live`: 14 passed (real Yahoo/Coinbase/Kraken/Binance candles in 1m/5m/1h, quotes, live engine collect, probes). Total 118 passed, 0 failed.

## Backtesting results
None yet (Milestone 6).

## Paper trading performance
No trades yet. Simulated equity: $1,000.00.

## Current system configuration
Paper only · internal_sim · $1,000 · 0.25 %/trade ($2.50) · 1 % daily loss ($10) · max 2 positions ·
R:R ≥ 1:2 · leverage/margin off · min score 70 · data: live ≤ 2 bars+60 s, stale > 5 bars,
crypto tolerance 50 bps, stock 100 bps, halt > no-majority divergence.

## Next development priorities (Milestone 3)
Indicators (VWAP, RSI, MACD, EMA 9/20/50, SMA 200, ATR, RVOL, Bollinger, S/R, breakouts, trend),
validated against reference values; stock scanner + crypto scanner on complete bars only;
first strategies (ORB, VWAP reclaim, momentum breakout, volume expansion, trend continuation);
structured signals with entry/stop/target/R:R and evidence stored in `signals`.
