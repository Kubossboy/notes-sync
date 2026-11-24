-- Analyzer version per news event: when tagging/catalyst rules improve, items are
-- re-analysed under the new version (append-only - old rows stay as audit trail)
-- and context queries only read the current version.
ALTER TABLE news_events ADD COLUMN analyzer_version INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_news_analyzer ON news_events(analyzer_version);
