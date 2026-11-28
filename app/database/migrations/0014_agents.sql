-- D105 multi-agent review: one row per candidate (latest evaluation wins), pruned with the other journals
CREATE TABLE IF NOT EXISTS agent_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    signal_id INTEGER NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    strategy TEXT,
    consensus_score INTEGER,
    consensus_stance TEXT,                -- APPROVE | NEUTRAL | REJECT
    size_mult REAL,
    enforced INTEGER NOT NULL DEFAULT 0,
    final_decision TEXT NOT NULL,         -- TRADE | NO_TRADE
    final_by TEXT,                        -- risk | market | technical | news | strategy | consensus
    final_reason TEXT,
    verdicts_json TEXT NOT NULL           -- {agent: {score, stance, reason, ...}} incl. the Risk Engine
);
CREATE INDEX IF NOT EXISTS ix_agent_reviews_ts ON agent_reviews(ts);
