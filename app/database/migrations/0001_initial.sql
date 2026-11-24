-- ATLAS Trading AI - initial schema (milestone 1)
-- All timestamps are ISO-8601 UTC strings. Every row that records a
-- decision is append-only: DELETE is blocked by triggers (spec section 8:
-- "Never silently delete losing trades or unsuccessful signals").

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('debug','info','warning','error','critical')),
    module TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    module TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT NOT NULL,
    traceback TEXT,
    resolved INTEGER NOT NULL DEFAULT 0
);

-- Key/value system state (e.g. safe-state flag). Updates are journaled to system_events.
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS integration_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    integration TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','degraded','unreachable','needs_credentials','disabled')),
    latency_ms REAL,
    http_status INTEGER,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS market_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_ts TEXT NOT NULL,          -- when ATLAS fetched it
    source_ts TEXT,                     -- timestamp reported by the source
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL CHECK (asset_class IN ('stock','crypto')),
    source TEXT NOT NULL,
    data_status TEXT NOT NULL CHECK (data_status IN ('live','delayed','cached','unavailable')),
    timeframe TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    bid REAL, ask REAL,
    is_complete_candle INTEGER,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_symbol_ts ON market_observations(symbol, observed_ts);

CREATE TABLE IF NOT EXISTS news_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_ts TEXT NOT NULL,
    published_ts TEXT,
    source TEXT NOT NULL,
    url TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    tickers TEXT,                       -- comma separated
    category TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    content_hash TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS sentiment_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT,
    source TEXT NOT NULL,
    method TEXT NOT NULL,
    score REAL NOT NULL CHECK (score >= -1 AND score <= 1),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    news_event_id INTEGER REFERENCES news_events(id),
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL CHECK (asset_class IN ('stock','crypto')),
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('long','short')),
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    reward_risk REAL,
    data_ts TEXT,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new','incomplete','rejected','accepted','expired','executed')),
    evidence_json TEXT
);

CREATE TABLE IF NOT EXISTS signal_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    ts TEXT NOT NULL,
    total_score REAL NOT NULL CHECK (total_score >= 0 AND total_score <= 100),
    technical REAL, momentum_volume REAL, news REAL, sentiment REAL, quant REAL, liquidity REAL,
    weights_json TEXT NOT NULL,
    data_quality TEXT
);

CREATE TABLE IF NOT EXISTS strategy_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    signal_id INTEGER REFERENCES signals(id),
    decision TEXT NOT NULL CHECK (decision IN ('trade','skip','reject','halt')),
    reason TEXT NOT NULL,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS risk_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    signal_id INTEGER REFERENCES signals(id),
    check_name TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0,1)),
    value REAL,
    limit_value REAL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS order_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,  -- idempotency key: prevents duplicate orders
    signal_id INTEGER REFERENCES signals(id),
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL CHECK (asset_class IN ('stock','crypto')),
    side TEXT NOT NULL CHECK (side IN ('buy','sell')),
    order_type TEXT NOT NULL,
    quantity REAL NOT NULL CHECK (quantity > 0),
    limit_price REAL,
    stop_loss REAL,
    take_profit REAL,
    position_size_usd REAL,
    planned_risk_usd REAL,
    execution_mode TEXT NOT NULL CHECK (execution_mode = 'paper'),
    provider TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','submitted','filled','partially_filled','cancelled','rejected','error'))
);

CREATE TABLE IF NOT EXISTS simulated_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    order_id INTEGER NOT NULL REFERENCES order_requests(id),
    fill_price REAL NOT NULL CHECK (fill_price > 0),
    quantity REAL NOT NULL CHECK (quantity > 0),
    fee_usd REAL NOT NULL DEFAULT 0,
    slippage_usd REAL NOT NULL DEFAULT 0,
    reference_price REAL,
    assumptions TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_ts TEXT NOT NULL,
    closed_ts TEXT,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL CHECK (asset_class IN ('stock','crypto')),
    strategy TEXT,
    direction TEXT NOT NULL CHECK (direction IN ('long','short')),
    quantity REAL NOT NULL CHECK (quantity > 0),
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,             -- mandatory stop-loss
    take_profit REAL NOT NULL,           -- mandatory target
    exit_price REAL,
    fees_usd REAL NOT NULL DEFAULT 0,
    slippage_usd REAL NOT NULL DEFAULT 0,
    realized_pnl_usd REAL,
    unrealized_pnl_usd REAL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    exit_reason TEXT,
    entry_order_id INTEGER REFERENCES order_requests(id),
    exit_order_id INTEGER REFERENCES order_requests(id)
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cash_usd REAL NOT NULL,
    positions_value_usd REAL NOT NULL,
    equity_usd REAL NOT NULL,
    realized_pnl_usd REAL NOT NULL,
    unrealized_pnl_usd REAL NOT NULL,
    day_start_equity_usd REAL,
    note TEXT
);

-- Append-only protection for audit tables.
CREATE TRIGGER IF NOT EXISTS trg_no_delete_signals BEFORE DELETE ON signals
BEGIN SELECT RAISE(ABORT, 'audit table signals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_signal_scores BEFORE DELETE ON signal_scores
BEGIN SELECT RAISE(ABORT, 'audit table signal_scores is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_decisions BEFORE DELETE ON strategy_decisions
BEGIN SELECT RAISE(ABORT, 'audit table strategy_decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_risk_checks BEFORE DELETE ON risk_checks
BEGIN SELECT RAISE(ABORT, 'audit table risk_checks is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_orders BEFORE DELETE ON order_requests
BEGIN SELECT RAISE(ABORT, 'audit table order_requests is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_fills BEFORE DELETE ON simulated_fills
BEGIN SELECT RAISE(ABORT, 'audit table simulated_fills is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_positions BEFORE DELETE ON positions
BEGIN SELECT RAISE(ABORT, 'audit table positions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_equity BEFORE DELETE ON equity_snapshots
BEGIN SELECT RAISE(ABORT, 'audit table equity_snapshots is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_errors BEFORE DELETE ON errors
BEGIN SELECT RAISE(ABORT, 'audit table errors is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_events BEFORE DELETE ON system_events
BEGIN SELECT RAISE(ABORT, 'audit table system_events is append-only'); END;
-- Fills are immutable once written.
CREATE TRIGGER IF NOT EXISTS trg_no_update_fills BEFORE UPDATE ON simulated_fills
BEGIN SELECT RAISE(ABORT, 'simulated_fills rows are immutable'); END;
