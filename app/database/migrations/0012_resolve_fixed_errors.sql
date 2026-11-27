-- D99: errors whose root cause is fixed are marked resolved so the dashboard stops showing them as active.
-- alpaca empty book after the close -> EmptyBook (source skipped, no error, no halt) + no stock quotes when closed
UPDATE errors SET resolved = 1 WHERE message LIKE 'alpaca: empty quote%';
-- overlapping news ingests racing on the same headline -> IntegrityError now treated as "already stored"
UPDATE errors SET resolved = 1 WHERE error_type = 'IntegrityError' AND message LIKE '%news_events.content_hash%';
-- local run of an old build against the old column name (code uses is_complete_candle since)
UPDATE errors SET resolved = 1 WHERE message LIKE '%no column named is_complete%';
