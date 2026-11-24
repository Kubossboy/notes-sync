# Decisions log

Reversible technical decisions made during development. Newest last.

| # | Date | Decision | Why | How to reverse |
|---|---|---|---|---|
| D1 | 2026-11-24 | Project at `/workspace/atlas-trading-ai/` (persistent box storage, not inside dist/build/out) | Those folder names may be wiped on box refresh | move folder |
| D2 | 2026-11-24 | Python 3.13.5 in a local `.venv` managed by `uv`, pinned `requirements.txt` | Reproducible, fast installs | delete `.venv`, re-run setup |
| D3 | 2026-11-24 | Minimal deps for M1: pydantic, pyyaml, requests, pandas, numpy, pytest. FastAPI/Streamlit deferred to M7 | Spec: "start simple"; box RAM is shared and tight | add when needed |
| D4 | 2026-11-24 | Settings in `config/default.yaml`, validated by frozen pydantic models with `extra="forbid"` | Typos and unsafe values fail at startup instead of silently | edit YAML / overlay via `ATLAS_CONFIG` |
| D5 | 2026-11-24 | Safety invariants are type-level: `mode: Literal["paper"]`, `leverage_enabled: Literal[False]`, `require_stop_loss: Literal[True]`, etc. | An unsafe config cannot even be loaded | intentionally not reversible without code change |
| D6 | 2026-11-24 | Config hash recorded at every start; changes stored in `config_snapshots` + `config_changed` event | "Never silently modify trading parameters" | — |
| D7 | 2026-11-24 | Secrets only from env vars / git-ignored `.env`, held as `SecretStr`; log redaction filter masks known secrets and token patterns | Never expose keys | — |
| D8 | 2026-11-24 | SQLite (WAL, `synchronous=FULL`, foreign keys on) with ordered SQL migrations | Spec default; single-process app | migrate to Postgres later if justified |
| D9 | 2026-11-24 | Audit tables are append-only via DELETE-blocking triggers; fills immutable; `order_requests.execution_mode CHECK = 'paper'`; `client_order_id UNIQUE` as idempotency key | Auditability, no duplicate orders, no deleted losers | new migration |
| D10 | 2026-11-24 | Money stored as REAL (float64) for now | Simulation at $1,000 scale; cents-level float error negligible | migrate to integer cents if needed |
| D11 | 2026-11-24 | SAFE STATE flag persisted in `system_state`; set on DB integrity failure or manual `halt`; cleared only by explicit `resume` (journaled) | Spec section 15 | — |
| D12 | 2026-11-24 | A database file that is not SQLite at all makes startup fail loudly (exception) rather than enter safe state, because the state cannot be stored in a broken DB | Fail closed | — |
| D13 | 2026-11-24 | Execution provider default = `internal_sim` (own simulator). Alpaca paper integration prepared but needs a free account + keys (not created) | No sign-ups without the user; simulator needs nothing | set keys in `.env`, `provider: alpaca_paper` |
| D14 | 2026-11-24 | Stock data: Yahoo chart endpoint (free, unofficial, no key). Treated as `delayed` unless timestamps prove freshness | Only reliable keyless US-equity intraday source reachable; Stooq returned 404 | swap provider in M2 |
| D15 | 2026-11-24 | Crypto data: Coinbase Exchange + Kraken + Binance public mirror (`data-api.binance.vision`); CoinGecko for fundamentals | All keyless and reachable; multiple sources allow cross-validation | — |
| D16 | 2026-11-24 | X/social engine disabled (`needs_credentials`); not claiming X monitoring | Spec: "Do not claim to monitor X unless a working API is configured"; X search needs a paid tier | add `X_BEARER_TOKEN` |
| D17 | 2026-11-24 | Crypto fee assumption 60 bps taker, slippage 10 bps crypto / 5 bps stocks, $0 stock commission | Conservative retail assumptions; configurable | edit YAML |
| D18 | 2026-11-24 | Live-network tests marked `@pytest.mark.live`; offline suite has no network | Deterministic CI + real verification | — |
| D19 | 2026-11-24 | Times stored in UTC ISO-8601; user zone Europe/Prague, market zone America/New_York in config | Unambiguous audit trail | — |
