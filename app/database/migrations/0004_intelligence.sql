-- Milestone 4: news intelligence, sentiment, insider transactions.
ALTER TABLE news_events ADD COLUMN source_tier INTEGER;
ALTER TABLE news_events ADD COLUMN scope TEXT;
ALTER TABLE news_events ADD COLUMN story_id INTEGER;
ALTER TABLE news_events ADD COLUMN verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE news_events ADD COLUMN verification TEXT;
ALTER TABLE news_events ADD COLUMN sentiment REAL;
ALTER TABLE news_events ADD COLUMN direction REAL;
ALTER TABLE news_events ADD COLUMN impact TEXT;
ALTER TABLE news_events ADD COLUMN catalysts_json TEXT;
ALTER TABLE news_events ADD COLUMN unexpected INTEGER NOT NULL DEFAULT 0;
ALTER TABLE news_events ADD COLUMN time_flags TEXT;
ALTER TABLE news_events ADD COLUMN tag_conf_json TEXT;
ALTER TABLE news_events ADD COLUMN external_id TEXT;
CREATE INDEX IF NOT EXISTS ix_news_published ON news_events(published_ts);
CREATE INDEX IF NOT EXISTS ix_news_story ON news_events(story_id);

CREATE TABLE IF NOT EXISTS news_ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts TEXT NOT NULL,
    finished_ts TEXT,
    sources_ok INTEGER, sources_failed INTEGER,
    items_seen INTEGER, items_new INTEGER,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS insider_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    accession TEXT NOT NULL,
    line INTEGER NOT NULL,
    owner TEXT, title TEXT, is_director INTEGER, is_officer INTEGER,
    code TEXT, shares REAL, price REAL, acq_disp TEXT, tx_date TEXT, plan_10b5_1 INTEGER,
    UNIQUE (accession, line)
);

CREATE TRIGGER IF NOT EXISTS trg_no_delete_news BEFORE DELETE ON news_events
BEGIN SELECT RAISE(ABORT, 'audit table news_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_sentiment BEFORE DELETE ON sentiment_results
BEGIN SELECT RAISE(ABORT, 'audit table sentiment_results is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_delete_insider BEFORE DELETE ON insider_transactions
BEGIN SELECT RAISE(ABORT, 'audit table insider_transactions is append-only'); END;
