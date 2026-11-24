ALTER TABLE positions ADD COLUMN initial_target REAL;
UPDATE positions SET initial_target = take_profit WHERE initial_target IS NULL;
ALTER TABLE shadow_positions ADD COLUMN initial_stop REAL;
ALTER TABLE shadow_positions ADD COLUMN initial_target REAL;
CREATE TRIGGER IF NOT EXISTS trg_pos_stop_never_widens BEFORE UPDATE OF stop_loss ON positions
WHEN NEW.stop_loss < OLD.stop_loss
BEGIN SELECT RAISE(ABORT, 'stop-loss may only tighten (risk can never grow)'); END;
CREATE TRIGGER IF NOT EXISTS trg_pos_target_never_extends BEFORE UPDATE OF take_profit ON positions
WHEN NEW.take_profit > OLD.take_profit
BEGIN SELECT RAISE(ABORT, 'take-profit may only be reduced'); END;
CREATE TRIGGER IF NOT EXISTS trg_pos_no_adding BEFORE UPDATE OF quantity ON positions
WHEN NEW.quantity <> OLD.quantity
BEGIN SELECT RAISE(ABORT, 'no averaging down / adding to positions'); END;
CREATE TRIGGER IF NOT EXISTS trg_shadow_stop_never_widens BEFORE UPDATE OF stop_loss ON shadow_positions
WHEN NEW.stop_loss < OLD.stop_loss
BEGIN SELECT RAISE(ABORT, 'stop-loss may only tighten (risk can never grow)'); END;
CREATE TRIGGER IF NOT EXISTS trg_shadow_target_never_extends BEFORE UPDATE OF take_profit ON shadow_positions
WHEN NEW.take_profit > OLD.take_profit
BEGIN SELECT RAISE(ABORT, 'take-profit may only be reduced'); END;
CREATE TABLE IF NOT EXISTS trade_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ledger TEXT NOT NULL CHECK (ledger IN ('main','shadow')),
    position_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('stop','target','exit')),
    rule TEXT NOT NULL,
    old_value REAL,
    new_value REAL,
    price REAL,
    r_now REAL,
    reason TEXT NOT NULL,
    alpaca_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_trade_adj_pos ON trade_adjustments(ledger, position_id);
CREATE TRIGGER IF NOT EXISTS trg_no_delete_trade_adj BEFORE DELETE ON trade_adjustments
BEGIN SELECT RAISE(ABORT, 'trade_adjustments is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_no_update_trade_adj BEFORE UPDATE ON trade_adjustments
BEGIN SELECT RAISE(ABORT, 'trade_adjustments is immutable'); END;
