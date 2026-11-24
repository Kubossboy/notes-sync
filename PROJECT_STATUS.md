# PROJECT STATUS – ATLAS Trading AI

_Last updated: 2026-11-24 (Europe/Prague)_

## Current development phase
Milestone 1 – Foundation: **complete**. Next: Milestone 2 – Market data.

## Environment (inspected 2026-11-24)
Debian 13 (trixie) x86_64, kernel 6.12, 8 vCPU, 15.6 GB RAM (shared, ~2.4 GB free
at inspection), ~100 GB free disk, Python 3.13.5, uv, git, node/npm, curl; no
docker, no sqlite3 CLI (Python's sqlite3 module is used). `/workspace` is persistent.
Outbound HTTPS works.

## Completed modules
- Project structure, pinned dependencies, setup/test scripts
- Configuration system (YAML + env secrets, strict validation, config hashing)
- Logging (JSON lines `logs/atlas.jsonl`, rotation, secret redaction)
- Database (17 tables, migrations, append-only audit triggers, integrity check)
- Safety layer (paper-only assertion, live-endpoint URL guard, persistent SAFE STATE)
- Environment inspection
- Integration registry with live health probes (13 sources)
- CLI: init, env, probe, status, halt, resume

## Remaining tasks
M2 market data → M3 scanners/strategies → M4 news/sentiment → M5 risk engine +
paper execution → M6 backtesting → M7 dashboard → M8 live paper trading.

## Known bugs / limitations
- Yahoo endpoint is unofficial and may change or rate-limit.
- CoinGecko keyless tier has low rate limits.
- SEC asks for a real contact e-mail in User-Agent; `ATLAS_CONTACT_EMAIL` not set yet (placeholder used).
- A non-SQLite (garbage) DB file aborts startup instead of entering SAFE STATE (by design, D12).

## API integrations (live probe 2026-11-24 09:06 Europe/Prague)
| Integration | Status |
|---|---|
| yahoo_finance_chart (stocks) | ok |
| binance_public, coinbase_exchange_public, kraken_public, coingecko_free (crypto) | ok |
| sec_edgar_submissions, sec_edgar_8k_feed, sec_edgar_form4_feed | ok |
| federal_reserve_rss, marketwatch_rss, coindesk_rss | ok |
| alpaca_paper | needs_credentials |
| x_api_v2 | needs_credentials |

## Required credentials (all optional)
- `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` – free Alpaca paper account (optional; internal simulator works without)
- `X_BEARER_TOKEN` – X API (search needs a paid tier)
- `ATLAS_CONTACT_EMAIL` – recommended for SEC EDGAR fair-access policy

## Test results
`pytest`: 60 passed (59 offline + 1 live network test against real free APIs).

## Backtesting results
None yet (Milestone 6).

## Paper trading performance
No trades yet. Simulated equity: $1,000.00.

## Current system configuration
Paper only · provider internal_sim · start equity $1,000 · risk/trade 0.25% ($2.50) ·
daily loss limit 1% ($10) · max 2 positions · min R:R 1:2 · leverage/margin off ·
min score 70 · weights 25/20/20/10/15/10 · config hash `780d20149bd46733`.

## Next development priorities (Milestone 2)
Stock + crypto OHLCV collectors (Yahoo, Coinbase/Kraken/Binance), normalised
candle model, timestamp/staleness validation (live/delayed/cached/unavailable),
incomplete-candle flagging, cross-source price sanity checks, storing
observations, SAFE STATE on invalid data, reliability tests.
