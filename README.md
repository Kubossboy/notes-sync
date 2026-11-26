# ATLAS Trading AI

**A**utonomous **T**rading & **M**arket **I**ntelligence **S**ystem: a modular
market-intelligence and **paper-trading-only** platform for US stocks and crypto
(day trading / short-term momentum, $1,000 simulated portfolio).

> **Paper trading only.** Live order execution, leverage, margin, martingale and
> averaging down are rejected by config validation, by URL guards in code, and by
> database constraints (`execution_mode` must equal `'paper'`). Nothing here can
> place a real-money order.

Current phase: **Milestone 7 – dashboard, forward paper test, Alpaca PAPER mirror**. See `PROJECT_STATUS.md`.

**New here / using another AI?** Read [`AGENTS.md`](AGENTS.md) (rules for any coding AI), [`START_CZ.md`](START_CZ.md) (Czech quick start)
and [`docs/CHATGPT_PROMPT.md`](docs/CHATGPT_PROMPT.md). Cross-platform launchers: `./start.sh <cmd>` (Linux/macOS), `start.bat <cmd>` (Windows),
`make <cmd>`; commands: `setup test status probe trade loop dashboard stop kill resume`.

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

Scanners & signals (Milestone 3):

```bash
.venv/bin/python -m app.main scan                    # live scan: stocks (5m) + crypto (15m), signals journaled
.venv/bin/python -m app.main scan --asset stock --research   # dry run on delayed data (market closed), nothing stored
.venv/bin/python -m app.main signals                 # recent journaled signals with scores
.venv/bin/python scripts/replay_signals.py BTC-USD NVDA      # how often each setup fired on recent real bars
```

Signals carry entry / stop / target, cost-aware net reward:risk, a 0-100 score
and confidence. Until the news/sentiment (M4) and validation (M6) modules exist,
signals are `incomplete` (max reachable score 55 < 60 = "do not trade") or
`rejected` (R:R < 2 after costs, bad stop, poor liquidity). No orders exist yet (M5).

News, catalysts & sentiment (Milestone 4):

```bash
.venv/bin/python -m app.main news ingest        # fetch RSS/Atom + SEC filings/Form 4, tag, classify, verify, journal
.venv/bin/python -m app.main news show NVDA     # 24 h context: aggregate sentiment, catalysts, verification, macro
.venv/bin/python -m app.main news latest        # newest items: time, source, tier, verified, sentiment, impact, tickers
.venv/bin/python -m app.main news macro         # Fed / rates / inflation / geopolitical items
.venv/bin/python -m app.main news health        # last ingest age + source success ratio
.venv/bin/python -m app.main scan --no-news     # scan without the news engine (news/sentiment then "incomplete")
```

The scanner re-ingests automatically when the last ingest is older than 15 min.
Every news event keeps its source, publication time (never invented; missing
times lower confidence) and a confidence value (source tier × time quality ×
ticker-match confidence). A story is **verified** when it comes from a primary
source (SEC, Fed, FDA, BLS, CFTC, exchange status pages) or from ≥ 2
independent outlets. News and sentiment now feed the score (news 20 %, sentiment
10 %); a verified high-impact negative catalyst vetoes longs. Quant validation
(M6) is still missing, so signals stay `incomplete` (max 85).

Risk engine & paper execution (Milestone 5):

```bash
.venv/bin/python -m app.main trade               # one cycle: manage open positions -> scan -> risk -> simulated fills -> equity snapshot
.venv/bin/python -m app.main trade --no-scan     # only manage stops/targets/time exits + mark-to-market
.venv/bin/python -m app.main portfolio           # equity, cash, total/daily P&L, daily loss limit, open positions
.venv/bin/python -m app.main positions --all     # trade history (entry, exit, P&L, R multiple, exit reason)
.venv/bin/python -m app.main orders              # order requests + fills (fees, slippage)
.venv/bin/python -m app.main risk                # risk evaluations and the rules that failed
.venv/bin/python -m app.main kill "reason" --flatten   # KILL SWITCH: SAFE STATE + close all paper positions
```

ATLAS keeps its **own $1,000 ledger** (internal simulator, authoritative). Every
trade is sized so the loss at the stop *including fees, spread and slippage* is
≤ 0.25 % of current equity ($2.50), notional ≤ 50 % of equity and ≤ cash (no
leverage). New entries stop at the 1 % daily loss limit ($10 of the day-start
equity), after 3 consecutive losses in a day, for 60 min in a symbol after a
loss, and whenever SAFE STATE is active. Only **complete** signals with score ≥ 70
reach the risk engine - until quant validation (M6) exists every signal is
`incomplete`, so **no paper trades happen yet by design**.

Simulation assumptions (also stored on every fill): market orders fill at the
ask/bid (or last ± 2 bps assumed half-spread) ± slippage (5 bps stocks / 10 bps
crypto); fees 10 bps crypto, 0 stocks; stops fill at min(stop, bar open) − slippage
(gaps hurt); targets fill only if price trades *above* the target; stop wins if
both are hit in one bar; stocks are flattened 5 min before the close; crypto max
hold 24 h. Real fills can be worse (partial fills, latency, queue position).

Alpaca **paper** order mirroring exists but is **off** (`execution.alpaca_paper_orders_enabled: false`);
read-only eligibility check: NVDA/AAPL/SPY/TSLA and BTC/ETH/SOL are tradable on the paper account.

Backtesting & validation (Milestone 6):

```bash
.venv/bin/python -m app.main backtest --asset crypto      # 2 y Coinbase 15m: IS/OOS, walk-forward, sensitivity, cost stress
.venv/bin/python -m app.main backtest --asset stock       # 2 y Alpaca IEX 5m (needs the Alpaca data keys)
.venv/bin/python -m app.main backtest --asset stock --source yahoo --days 60   # robustness cross-check, not persisted
.venv/bin/python -m app.main backtest --strategies momentum_breakout --json var/bt.json
.venv/bin/python -m app.main validation                  # latest verdict per strategy = what the quant score sees
```

The backtester replays the **live** strategy code, plan gate, cost/fill model and
position sizing bar by bar (entry at the next bar's open, stop assumed first,
gaps hurt, same portfolio/risk limits). Each strategy gets an in-sample (first
60 %) and out-of-sample (last 40 %) result, a 5-fold walk-forward, loose/strict
neighbour parameters and a ×1.5 cost stress. A strategy is **validated** only if
the out-of-sample result with the live parameters reaches ≥ 30 trades, profit
factor ≥ 1.3, positive expectancy, max drawdown < 5 %, ≥ 60 % positive walk-forward
folds, PF > 1 under cost stress and for both neighbours. Failed strategies make
their signals `rejected`; strategies with too little data, not backtestable
(news / order-book) or with a stale (> 30 d) or outdated validation leave the
quant component empty → `incomplete` → never executed. Walk-forward winners are
reported, never auto-deployed. Results: `docs/BACKTEST_RESULTS.md`.

News-driven forward paper test (after M6): technical strategies failed validation and stay
rejected. Decisions are now driven mainly by **verified** news/catalysts (score weights news 40 /
sentiment 15 / quant 15 / liquidity 10 / technical 10 / momentum 10; technicals only confirm the
entry). `news_momentum` cannot be backtested (no free news history), so it runs as a clearly
labelled **forward_test** on the internal $1,000 ledger only (never Alpaca orders), with all M5
risk limits. Every forward-test trade is logged in `forward_test_trades`; judge after ≥ 30 trades:

```bash
.venv/bin/python -m app.main forwardtest     # stats + last trades
```

Dashboard (Milestone 7) - local, read-only:

```bash
.venv/bin/python -m app.main dashboard            # http://127.0.0.1:8765  (JSON: /api/state)
```

Overview (simulated equity, daily/total P&L, open positions, daily loss limit, safe state, health),
market scanner (ticker, asset class, data time, score, strategy, entry/stop/target, R:R, confidence,
evidence or reject reason), portfolio, analytics (win rate, PF, max DD, avg winner/loser,
expectancy, by strategy and asset class), forward paper test, strategy validation verdicts, news
intelligence, risk decisions and system status (integrations, last updates, missing data, errors,
active modules). SIMULATED and REAL panels are labelled.

Run continuously (paper only; not started automatically):

```bash
nohup ./scripts/run_loop.sh >/dev/null 2>&1 &             # trade cycle every 60 s (INTERVAL=...), logs/loop.log
nohup .venv/bin/python -m app.main dashboard >/dev/null 2>&1 &
touch var/STOP                                             # stop the loop after the current cycle
```

Other commands: `python -m app.main halt <reason>` (enter SAFE STATE: no new
simulated trades) and `python -m app.main resume --by <name>` (journaled).


Dashboard v2: 5 summary cards, a live candle chart per open position (2 positions = side by side), a simulated equity curve, and collapsed detail panels; top section auto-refreshes every 30 s.
DEMO of the 2-position layout (throwaway /tmp DB copy, never the real ledger): `.venv/bin/python scripts/dashboard_demo.py 60` then open http://127.0.0.1:8799/.

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
  technical/         indicators (VWAP, RSI, MACD, EMA 9/20/50, SMA 200, ATR, RVOL, Bollinger,
                     S/R, volume profile, breakouts, trend, candlestick patterns)
  strategies/        stock.py (7) and crypto.py (7) strategy modules
  scanners/          scanner (data -> indicators -> strategies -> scored signals) + scoring technical/ fundamentals/ intelligence/ sentiment/
  backtest/          history.py (free historical bars, cached), engine.py (replay), validation.py (IS/OOS/WF)
  quant/             engine.py: validation verdict -> quant score component
  risk/ execution/ portfolio/   risk engine, paper simulator/trader, ledger
  dashboard/         data.py (read-only journal view), server.py (local web dashboard)
  analytics/         (later milestones)
config/default.yaml  all non-secret settings; optional overlay via ATLAS_CONFIG (config/scalp.yaml = scalp profile, D95)
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
| Federal Reserve RSS (monetary + all press) | FOMC / macro (tier 1) | no |
| BLS, FDA, SEC press, CFTC RSS | macro / regulatory (tier 1) | no |
| Coinbase + Kraken status pages (Atom/RSS) | exchange incidents, maintenance, listings (tier 1) | no |
| Yahoo Finance headline RSS (per ticker) | company news (tier 2) | no |
| MarketWatch top + pulse, PR Newswire RSS | market news / press releases (tier 2) | no |
| CoinDesk, Cointelegraph, Decrypt RSS | crypto news (tier 3) | no |
| Alpaca market data (IEX feed) | 2nd stock source, bid/ask | keys set via env (paper account) |
| Alpaca paper | paper account (execution in M5) | keys set via env |
| X API v2 recent search | social sentiment (tier 4, never verifies alone) | **needs credentials (paid tier for search)** – module inactive, ATLAS does not claim to monitor X |
| Finnhub / NewsAPI | optional extra news | needs free keys – not used (no sign-ups) |

## Credentials

Never paste keys into chat. Put them in `/workspace/atlas-trading-ai/.env`
(git-ignored, see `.env.example`) directly on the box. Missing keys only
disable the related module (`needs_credentials`); everything else keeps working.

## Disclaimer

Research software. Paper results are simulations with stated assumptions
(fees, slippage) and are not evidence of future profitability.

## Publishing a read-only snapshot (Cloudflare Pages, free)
- Export only (no network deploy): `.venv/bin/python -m app.main export --out var/site` -> prints project + secret path.
- Deploy once: with `CLOUDFLARE_API_TOKEN` (Pages:Edit) in the process env (never in .env), run `scripts/publish.sh`.
- Fallback without a token: `scripts/worker_bundle.py` (Worker deploy through the Cloudflare MCP connection).
- Every 5 min via cron: `*/5 * * * * cd /workspace/atlas-trading-ai && scripts/publish.sh >> logs/publish.log 2>&1`
  (or `scripts/publish_loop.sh`, stop with `touch var/STOP_PUBLISH`). Not started automatically.
- URL: `https://<project>.pages.dev/<slug>/` (both in git-ignored `var/publish.json`). Scrubbed, noindex, no controls.

## Alpaca PAPER mirror (D85)
Manual owner trades: `python -m app.main trade-manual SYMBOL --stop X --target Y --news-id N --dry-run` (D87).
`execution.alpaca_paper_orders_enabled: true` - internal trades (incl. forward_test) are mirrored to paper-api.alpaca.markets with the
qty sized from the ATLAS $1,000 ledger. The internal ledger stays authoritative. Live trading is blocked in code.
