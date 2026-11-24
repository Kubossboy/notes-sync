-- Shadow forward test (D91): every strategy, incl. failed / unavailable ones, paper-trades on a separate
-- shadow ledger. It never touches the $1,000 portfolio, the risk engine or Alpaca.
CREATE TABLE IF NOT EXISTS shadow_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_ts TEXT NOT NULL,
    signal_id INTEGER NOT NULL UNIQUE REFERENCES signals(id),
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    strategy TEXT NOT NULL,
    timeframe TEXT,
    validation_status TEXT,
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    quantity REAL NOT NULL,
    planned_risk_usd REAL NOT NULL,
    entry_fee_usd REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    last_managed_bar TEXT,
    mark_price REAL,
    closed_ts TEXT,
    exit_price REAL,
    exit_reason TEXT,
    pnl_usd REAL,
    r_multiple REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_shadow_open ON shadow_positions(strategy, symbol) WHERE status = 'open';
CREATE TRIGGER IF NOT EXISTS trg_no_delete_shadow_pos BEFORE DELETE ON shadow_positions
BEGIN SELECT RAISE(ABORT, 'shadow_positions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_shadow_closed_immutable BEFORE UPDATE ON shadow_positions
WHEN OLD.status = 'closed'
BEGIN SELECT RAISE(ABORT, 'closed shadow positions are immutable'); END;

CREATE TABLE IF NOT EXISTS shadow_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    shadow_position_id INTEGER NOT NULL UNIQUE REFERENCES shadow_positions(id),
    signal_id INTEGER,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    strategy TEXT NOT NULL,
    opened_ts TEXT NOT NULL,
    closed_ts TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    quantity REAL NOT NULL,
    planned_risk_usd REAL NOT NULL,
    fees_usd REAL NOT NULL,
    pnl_usd REAL NOT NULL,
    r_multiple REAL NOT NULL,
    exit_reason TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_no_delete_shadow_trades BEFORE DELETE ON shadow_trades
BEGIN SELECT RAISE(ABORT, 'shadow_trades is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_update_shadow_trades BEFORE UPDATE ON shadow_trades
BEGIN SELECT RAISE(ABORT, 'shadow_trades is immutable'); END;

CREATE TABLE IF NOT EXISTS strategy_status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    strategy TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL CHECK (to_status IN ('shadow', 'eligible')),
    n_trades INTEGER NOT NULL,
    window_n INTEGER NOT NULL,
    profit_factor REAL,
    expectancy_r REAL,
    max_drawdown_usd REAL,
    reason TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_no_delete_status_log BEFORE DELETE ON strategy_status_log
BEGIN SELECT RAISE(ABORT, 'strategy_status_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_update_status_log BEFORE UPDATE ON strategy_status_log
BEGIN SELECT RAISE(ABORT, 'strategy_status_log is immutable'); END;
