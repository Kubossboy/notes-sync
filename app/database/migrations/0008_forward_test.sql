-- Part A (post-M6): forward paper test for strategies that cannot be backtested (news-driven).
-- validation_status: validated | forward_test | failed | unavailable
ALTER TABLE signals ADD COLUMN validation_status TEXT;
ALTER TABLE positions ADD COLUMN validation_status TEXT;

CREATE TABLE IF NOT EXISTS forward_test_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    position_id INTEGER NOT NULL UNIQUE REFERENCES positions(id),
    signal_id INTEGER REFERENCES signals(id),
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    strategy TEXT NOT NULL,
    opened_ts TEXT NOT NULL,
    closed_ts TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    quantity REAL NOT NULL,
    planned_risk_usd REAL,
    fees_usd REAL NOT NULL,
    pnl_usd REAL NOT NULL,
    r_multiple REAL,
    exit_reason TEXT NOT NULL,
    catalysts_json TEXT
);
CREATE TRIGGER IF NOT EXISTS trg_no_delete_forward_test BEFORE DELETE ON forward_test_trades
BEGIN SELECT RAISE(ABORT, 'audit table forward_test_trades is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_update_forward_test BEFORE UPDATE ON forward_test_trades
BEGIN SELECT RAISE(ABORT, 'audit table forward_test_trades is immutable'); END;
