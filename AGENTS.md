# AGENTS.md - instructions for any coding AI (ChatGPT/Codex, Claude, Cursor, Copilot, Gemini, ...)

Read this whole file before changing anything. `CLAUDE.md` and `.github/copilot-instructions.md` point here.

## What ATLAS is
ATLAS Trading AI: a modular market-intelligence and **PAPER-TRADING-ONLY** system for US stocks and crypto
(short-term momentum, news-driven). It scans free data sources, scores signals, applies a strict risk engine and
simulates fills on an internal **$1,000 ledger** (SQLite, append-only journal). Approved trades are optionally mirrored
to the **Alpaca PAPER** API (`paper-api.alpaca.markets`) sized from that $1,000 ledger. The internal ledger is the source of truth.

## HARD RULES (never change, never work around)
1. **Paper only.** Never add live brokerage execution. `api.alpaca.markets` (live) and any real-money endpoint stay on the
   forbidden URL list (`app/config.py`, `app/core/safety.py`). `execution_mode` must stay `'paper'` (DB constraint).
2. **No crypto wallets / on-chain trading.** Never read, create or use private keys, seed phrases or wallet files.
3. **Risk limits are fixed** (config validation enforces them): risk per trade 0.25 % ($2.50 on $1,000), daily loss stop 1 %
   ($10), max 2 open positions, no leverage, no margin, no shorting, no martingale / averaging down, net reward:risk >= 2 after
   costs (`crypto_fee_bps` 10). Do not loosen them, and do not "tune" strategies to fit backtests.
4. **Honesty.** Never fabricate data, fills, P&L or backtest results. Missing data -> label it `unavailable` / `needs_credentials`.
   Strategies that FAILED validation stay rejected (`docs/BACKTEST_RESULTS.md`).
5. **Free data sources only.** No paid APIs, no sign-ups that need a card or deposit.
6. **Secrets.** Keys live only in `.env` (git-ignored) or the process environment. Never print, log, commit or paste them.
   Never commit `.env`, `var/`, `logs/`, `.venv/`. `.env.example` keeps names with EMPTY values.
7. **No outbound messages.** The code must not send e-mails, chats or posts.
8. A real-money trade is out of scope entirely. Changing any of the above requires the owner's explicit written approval.

## Architecture (app/)
| Package | Role |
|---|---|
| `config.py`, `config/default.yaml` | pydantic settings + validation (risk limits, forbidden URLs) |
| `core/` | bootstrap, safety guards (URL allow-list, SAFE STATE), environment |
| `database/` | SQLite + migrations 0001-0008, append-only triggers |
| `data/` | market-data providers (Coinbase, Kraken, Yahoo, Alpaca IEX ...), calendar, cross-checks |
| `intelligence/`, `sentiment/`, `fundamentals/` | news ingest (RSS, SEC EDGAR), catalysts, sentiment |
| `technical/`, `strategies/`, `scanners/`, `quant/` | indicators, strategies, scanners, validation-aware quant score |
| `risk/` | risk engine - every check must pass before a simulated entry |
| `execution/` | `simulator.py` (internal fills), `trader.py` (cycle), `alpaca_paper.py` + `mirror.py` (PAPER mirror) |
| `backtest/` | M6 validation: OOS split, walk-forward, cost stress |
| `dashboard/` | read-only localhost dashboard + static export for publishing |
Docs: `README.md`, `docs/ARCHITECTURE.md`, `DECISIONS.md` (numbered decisions D1..), `PROJECT_STATUS.md`.

## Setup / run / test / stop
Python 3.12+ (verified on 3.13). From the repo root:
```bash
./start.sh setup        # Windows: start.bat setup   (creates .venv, installs requirements.txt, inits DB)
cp .env.example .env    # then fill in only what you have; everything is optional
./start.sh test         # offline unit tests (no network)
./start.sh status       # config, risk limits, safe state
./start.sh dashboard    # http://127.0.0.1:8765 (read-only, localhost only)
./start.sh loop         # 24/7 paper loop: one `trade` cycle every 60 s (foreground; Ctrl+C or stop)
./start.sh stop         # creates var/STOP -> loop exits after the current cycle
./start.sh kill "why"   # KILL SWITCH: SAFE STATE + flatten all paper positions
```
Equivalent raw commands: `python -m app.main <init|status|probe|scan|trade|portfolio|positions|risk|kill|resume|backtest|validation|forwardtest|dashboard|export>`.
`make help` lists the same targets.

## Config & secrets
- Behaviour: `config/default.yaml` (validated on load; invalid or unsafe values abort).
- Secrets: `.env` (copy of `.env.example`) or env vars: `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` (PAPER keys),
  `ATLAS_CONTACT_EMAIL` (SEC User-Agent), optional `X_BEARER_TOKEN`, `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` (publishing).
- Runtime state: `var/atlas.sqlite3` (the ledger - never edit/delete rows by hand; tables are append-only), logs in `logs/`.

## Working rules for AI agents
- Run `./start.sh test` (or `python -m pytest -m "not live" -q`) before and after changes; keep all tests passing.
- Add a `DECISIONS.md` entry for any behaviour change; keep code/docs in English.
- Never start/stop a running loop or modify `var/` unless the owner asks. Never deploy, push or publish without the owner's OK.
- A code-only sandbox (e.g. ChatGPT code interpreter) cannot run the 24/7 loop - see `docs/CHATGPT_PROMPT.md`.
