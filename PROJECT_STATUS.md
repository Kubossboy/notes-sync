# PROJECT STATUS – ATLAS Trading AI

_Last updated: 2026-11-24 (Europe/Prague)_

## Current development phase
Milestone 3 – Scanners and strategies: **complete**. Next: Milestone 4 – Intelligence (news, X, sentiment, catalysts).

## Completed modules
- M1 foundation: config (strict, paper-only), logging + redaction, SQLite journal (append-only), safety/SAFE STATE, probes, CLI.
- M2 market data: Yahoo / Coinbase / Kraken / Binance providers, normalization, validation, freshness labels, cross-checks, cache, reliability.
- M3:
  - Alpaca market data (IEX) as 2nd stock source + Alpaca paper account probe (keys from env; disabled without keys)
  - Public L2 order books (Coinbase, Kraken, Binance) with spread/depth/imbalance + snapshots
  - Indicators: VWAP, RSI, MACD, EMA 9/20/50, SMA 200, ATR, RVOL (+ time-of-day RVOL), Bollinger, S/R, volume profile, breakouts, trend, candlestick patterns
  - Stock strategies: Opening Range Breakout, VWAP Reclaim, Momentum Breakout, Relative Volume Anomaly, Trend Continuation, S/R Breakout, News Momentum (disabled until M4)
  - Crypto strategies: Momentum Breakout, Volume Expansion, Order Book Imbalance, Volatility Expansion, Trend Continuation, S/R Breakout, Sudden Liquidity Change (+ liquidity-drop alert)
  - Scoring 0-100 with configurable weights, cost-aware net R:R, stop sanity, liquidity filters, confidence
  - Scanner (live + research dry-run), signal journal (signals, signal_scores, strategy_decisions, scan_runs), duplicate protection
  - CLI: scan, signals; scripts/replay_signals.py

## Remaining tasks
M4 intelligence → M5 risk engine + paper execution → M6 backtesting/validation → M7 dashboard → M8 live paper trading.

## Known bugs / limitations
- All signals are `incomplete` or `rejected` by design until M4 (news/sentiment) and M6 (quant validation): max reachable score 55 < 60.
- Strategy parameters are unvalidated hypotheses. Replay on ~3 days of real data: crypto setups fired but none reached net R:R ≥ 2 after 20 bps/side costs; stocks (NVDA, TSLA) had several that did. Not evidence of edge.
- Stock live scan could not run on live data today (US market closed during all runs); stock strategies were exercised on real delayed data (research mode) and in unit tests.
- Alpaca IEX feed: partial volume and wide after-hours quotes (excluded from consensus > 200 bps). Alpaca hourly bars are clock-aligned, Yahoo's are session-anchored, so 1h cross-checks rarely overlap.
- Alpaca paper account shows the Alpaca default $100,000 equity; ATLAS will keep its own $1,000 ledger (decision for M5).
- Order-book imbalance is a single snapshot and can be spoofed; liquidity-change needs ≥ 5 stored snapshots (builds up over scans).
- NYSE calendar hard-coded 2025-2027.

## API integrations (probe 2026-11-24 ~09:40 Europe/Prague): 13/14 ok
Yahoo, Alpaca market data (IEX), Alpaca paper account (ACTIVE), Binance, Coinbase, Kraken, CoinGecko, SEC EDGAR ×3, Fed RSS, MarketWatch RSS, CoinDesk RSS: ok. X API v2: needs_credentials.

## Required credentials
- Alpaca: set via env vars (present). - SEC contact e-mail: set in `.env`. - X API bearer token: missing (paid tier for search).

## Test results
Offline: 154 passed. Live (real APIs): 22 passed (incl. Alpaca data + paper account, Yahoo↔Alpaca cross-check, real order books, live crypto scan, research stock scan). Total 176 passed, 0 failed.

## Backtesting results
None yet (Milestone 6).

## Paper trading performance
No trades (execution arrives in M5). Simulated equity: $1,000.00.

## Current system configuration
Paper only · internal_sim · $1,000 · 0.25 %/trade ($2.50) · daily loss 1 % ($10) · max 2 positions · R:R ≥ 2 (net of costs) ·
leverage/margin off · min score 70 · weights 25/20/20/10/15/10 · stocks 5m / crypto 15m · crypto fee 10 bps + 10 bps slippage per side ·
stock 0 fee + 5 bps slippage · target 2.5 R capped by resistance.

## Next development priorities (Milestone 4)
News ingestion (SEC 8-K/Form 4, Fed, RSS) with source/time/confidence, ticker tagging, catalyst detection,
rule-based + lexicon sentiment, source verification and dedupe, X integration only if credentials exist,
wire news/sentiment components into scoring and enable News-Driven Momentum.
