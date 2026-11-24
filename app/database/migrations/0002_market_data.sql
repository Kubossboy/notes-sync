-- Milestone 2: market-data persistence.
ALTER TABLE market_observations ADD COLUMN session TEXT;
ALTER TABLE market_observations ADD COLUMN quote_currency TEXT;
ALTER TABLE market_observations ADD COLUMN kind TEXT NOT NULL DEFAULT 'candle';
-- one row per source/symbol/timeframe/bar; quotes are never deduplicated
CREATE UNIQUE INDEX IF NOT EXISTS ux_obs_candle ON market_observations(source, symbol, timeframe, source_ts) WHERE kind = 'candle';
CREATE INDEX IF NOT EXISTS ix_obs_kind_symbol ON market_observations(kind, symbol, timeframe, source_ts);

CREATE TABLE IF NOT EXISTS crosscheck_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- quote | candle
    consensus_price REAL,
    sources_json TEXT NOT NULL,
    max_deviation_bps REAL,
    outliers TEXT,
    verdict TEXT NOT NULL CHECK (verdict IN ('agree','single_source','outlier_excluded','divergent','unavailable'))
);
CREATE TRIGGER IF NOT EXISTS trg_no_delete_crosscheck BEFORE DELETE ON crosscheck_results
BEGIN SELECT RAISE(ABORT, 'audit table crosscheck_results is append-only'); END;
