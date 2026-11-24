-- Milestone 3: scanners, strategies, signals.
ALTER TABLE signals ADD COLUMN timeframe TEXT;
ALTER TABLE signals ADD COLUMN signal_key TEXT;
ALTER TABLE signals ADD COLUMN expires_ts TEXT;
ALTER TABLE signals ADD COLUMN data_status TEXT;
ALTER TABLE signals ADD COLUMN scan_run_id INTEGER REFERENCES scan_runs(id);
ALTER TABLE signals ADD COLUMN reject_reason TEXT;
-- the same setup on the same bar is stored once (prevents duplicate signals)
CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_key ON signals(signal_key);
CREATE INDEX IF NOT EXISTS ix_signals_symbol_ts ON signals(symbol, ts);

ALTER TABLE signal_scores ADD COLUMN available_weight REAL;
ALTER TABLE signal_scores ADD COLUMN score_on_available REAL;

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts TEXT NOT NULL,
    finished_ts TEXT,
    scanner TEXT NOT NULL,            -- stock | crypto
    mode TEXT NOT NULL CHECK (mode IN ('live','research')),
    timeframe TEXT NOT NULL,
    symbols TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    summary_json TEXT
);
CREATE TRIGGER IF NOT EXISTS trg_no_delete_scan_runs BEFORE DELETE ON scan_runs
BEGIN SELECT RAISE(ABORT, 'audit table scan_runs is append-only'); END;

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    best_bid REAL NOT NULL,
    best_ask REAL NOT NULL,
    spread_bps REAL NOT NULL,
    bid_depth_05_usd REAL, ask_depth_05_usd REAL,
    bid_depth_1_usd REAL, ask_depth_1_usd REAL,
    imbalance_05 REAL,
    levels INTEGER
);
CREATE INDEX IF NOT EXISTS ix_book_symbol_ts ON orderbook_snapshots(symbol, source, ts);
