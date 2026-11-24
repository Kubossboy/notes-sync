ALTER TABLE positions ADD COLUMN leverage REAL NOT NULL DEFAULT 1;
ALTER TABLE positions ADD COLUMN margin_usd REAL;
ALTER TABLE positions ADD COLUMN liquidation_price REAL;
UPDATE positions SET margin_usd = quantity * entry_price WHERE margin_usd IS NULL;
