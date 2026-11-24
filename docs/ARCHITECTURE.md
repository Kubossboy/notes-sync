# Architecture (Milestone 1)

```
CLI (app/main.py)
  └─ core.bootstrap ─┬─ config.load_settings / load_secrets   (validated, frozen)
                     ├─ core.logging_setup                    (JSON lines + redaction)
                     ├─ core.safety.assert_paper_only         (refuses unsafe config)
                     ├─ database.Database.migrate + integrity (SQLite, append-only audit)
                     └─ core.safety.SafeState                 (persistent halt flag)
integrations.registry ── probe_all → integration_health table
```

Data flow planned for later milestones:
market data → normalisation/validation → indicators/strategies → signals →
scoring (0-100, configurable weights) → deterministic risk engine → paper
execution → positions/equity → analytics/dashboard. The risk engine is pure
deterministic code; any AI/LLM component may only *propose* signals.

Safety layers (defence in depth):
1. Config types (`Literal["paper"]`, `Literal[False]` for leverage/margin).
2. `assert_url_allowed` blocks live order endpoints (Alpaca live, Coinbase
   brokerage orders, Binance order, Kraken private).
3. DB `CHECK (execution_mode = 'paper')`, unique `client_order_id`,
   mandatory `stop_loss`/`take_profit` on positions.
4. SAFE STATE stops new simulated trades on data/DB/integration failures.
