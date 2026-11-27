-- D104 adaptive learning: post-trade reviews and the learning log (pauses, size changes, parameter changes)
CREATE TABLE IF NOT EXISTS trade_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ledger TEXT NOT NULL,                 -- main | shadow
    position_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    strategy TEXT,
    regime TEXT,
    outcome TEXT NOT NULL,                -- win | loss
    r_multiple REAL,
    exit_reason TEXT,
    features_json TEXT,
    causes TEXT,                          -- comma list of rule tags
    lesson TEXT,                          -- optional one-sentence AI lesson
    UNIQUE (ledger, position_id)
);
CREATE INDEX IF NOT EXISTS ix_trade_reviews_strategy ON trade_reviews(strategy, ts);
CREATE TABLE IF NOT EXISTS learning_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- pause | resume | reduce | restore | param_change | param_revert
    strategy TEXT,
    detail TEXT NOT NULL,                 -- plain-English sentence
    data_json TEXT
);
