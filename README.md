# ATLAS Trading AI

**A**utonomous **T**rading & **M**arket **I**ntelligence **S**ystem: a modular
market-intelligence and **paper-trading-only** platform for US stocks and crypto
(day trading / short-term momentum, $1,000 simulated portfolio).

> **Paper trading only.** Live order execution, leverage, margin, martingale and
> averaging down are rejected by config validation, by URL guards in code, and by
> database constraints (`execution_mode` must equal `'paper'`). Nothing here can
> place a real-money order.

Current phase: **Milestone 2 – Market data** (complete). See `PROJECT_STATUS.md`.

## Quick start

```bash
cd /workspace/atlas-trading-ai
./scripts/setup.sh                 # venv + pinned deps + database
.venv/bin/python -m app.main env     # inspect host environment
.venv/bin/python -m app.main probe   # live-check every free data source
.venv/bin/python -m app.main status  # config, risk limits, safe state, last probes
./scripts/test.sh                  # offline tests, then live-network tests
```

Market data (Milestone 2):

```bash
.venv/bin/python -m app.main candles BTC-USD --tf 5m   # fetch, validate, label, store
.venv/bin/python -m app.main candles AAPL --tf 1h
.venv/bin/python -m app.main quote ETH-USD             # bid/ask from every source + consensus
.venv/bin/python -m app.main collect                   # whole watchlist (config/default.yaml)
.venv/bin/python -m app.main reliability --rounds 3    # success rate + latency per source
```

Every series is labelled `live` / `delayed` / `cached` / `unavailable`, the
still-forming bar is flagged `FORMING`, and only `live`, non-stale, valid data
counts as *tradable*. Invalid data or a total data outage puts ATLAS into SAFE
STATE (`status` shows the reason; `resume --by <name>` clears it).

Other commands: `python -m app.main halt <reason>` (enter SAFE STATE: no new
simulated trades) and `python -m app.main resume --by <name>` (journaled).

## Layout

```
app/
  main.py            CLI entry point
  config.py          validated settings (YAML) + secrets (env only)
  core/              bootstrap, logging (JSON + redaction), safety/safe state, env inspection
  database/          SQLite layer + SQL migrations (append-only audit tables)
  integrations/      data-source registry + health probes
  data/              market data engine: models, providers (yahoo, coinbase, kraken, binance),
                     validation, freshness labels, cross-checks, NYSE calendar, reliability
  scanners/ technical/ fundamentals/ intelligence/ sentiment/
  quant/ risk/ execution/ portfolio/ analytics/ dashboard/   (later milestones)
config/default.yaml  all non-secret settings
.env.example         optional credentials (copy to .env, never commit)
tests/               pytest suite (`-m live` = real network)
docs/                architecture notes
logs/  var/          runtime logs and the SQLite database (git-ignored)
```

## Data sources (all free, no payment card)

| Source | Use | Key? |
|---|---|---|
| Yahoo Finance chart API | US stock prices/candles | no (unofficial) |
| Binance public (data-api.binance.vision) | crypto prices, order book | no |
| Coinbase Exchange public | crypto ticker incl. bid/ask | no |
| Kraken public | crypto ticker | no |
| CoinGecko | crypto market cap / supply | no (keyless tier, low limits) |
| SEC EDGAR (submissions, 8-K, Form 4) | filings, insider disclosures | no (contact e-mail in User-Agent) |
| Federal Reserve RSS | FOMC / macro | no |
| MarketWatch, CoinDesk RSS | headlines | no |
| Alpaca paper | paper broker (optional) | **needs free account + keys** |
| X API v2 | social sentiment | **needs credentials (paid tier for search)** |

## Credentials

Never paste keys into chat. Put them in `/workspace/atlas-trading-ai/.env`
(git-ignored, see `.env.example`) directly on the box. Missing keys only
disable the related module (`needs_credentials`); everything else keeps working.

## Disclaimer

Research software. Paper results are simulations with stated assumptions
(fees, slippage) and are not evidence of future profitability.
