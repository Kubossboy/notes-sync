-- Milestone 6: backtest / validation journal (append-only audit tables).
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    batch_id TEXT NOT NULL,                 -- one `backtest run` invocation
    asset_class TEXT NOT NULL,
    strategy TEXT NOT NULL,
    source TEXT,
    timeframe TEXT,
    symbols TEXT,
    period_start TEXT,
    period_end TEXT,
    oos_start TEXT,
    params_json TEXT NOT NULL,              -- the default (deployed) parameters that were validated
    params_hash TEXT NOT NULL,
    cost_model_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    app_version TEXT NOT NULL,
    metrics_json TEXT NOT NULL,             -- in_sample / out_of_sample / walk_forward / sensitivity / stress
    verdict TEXT NOT NULL CHECK (verdict IN ('validated', 'failed', 'insufficient_data', 'not_backtestable')),
    reasons_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_strategy ON backtest_runs(asset_class, strategy, id);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES backtest_runs(id),
    segment TEXT NOT NULL,                  -- in_sample | out_of_sample | wf_fold_N | stress_oos
    variant TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_ts TEXT NOT NULL,
    entry_ts TEXT NOT NULL,
    exit_ts TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    quantity REAL NOT NULL,
    fees_usd REAL NOT NULL,
    pnl_usd REAL NOT NULL,
    r_multiple REAL NOT NULL,
    exit_reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_backtest_trades_run ON backtest_trades(run_id);

CREATE TRIGGER IF NOT EXISTS trg_no_delete_backtest_runs BEFORE DELETE ON backtest_runs
BEGIN SELECT RAISE(ABORT, 'audit table backtest_runs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_update_backtest_runs BEFORE UPDATE ON backtest_runs
BEGIN SELECT RAISE(ABORT, 'audit table backtest_runs is immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_backtest_trades BEFORE DELETE ON backtest_trades
BEGIN SELECT RAISE(ABORT, 'audit table backtest_trades is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_update_backtest_trades BEFORE UPDATE ON backtest_trades
BEGIN SELECT RAISE(ABORT, 'audit table backtest_trades is immutable'); END;
