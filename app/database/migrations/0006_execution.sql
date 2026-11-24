-- Milestone 5: risk engine + paper execution.
ALTER TABLE risk_checks ADD COLUMN evaluation_id TEXT;
CREATE INDEX IF NOT EXISTS ix_risk_eval ON risk_checks(evaluation_id);

CREATE TABLE IF NOT EXISTS risk_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    signal_id INTEGER REFERENCES signals(id),
    symbol TEXT NOT NULL,
    approved INTEGER NOT NULL CHECK (approved IN (0,1)),
    quantity REAL, notional_usd REAL, planned_risk_usd REAL, risk_budget_usd REAL,
    expected_fill REAL, est_fees_usd REAL, rr_net REAL,
    equity_usd REAL, cash_usd REAL, day_start_equity_usd REAL, daily_loss_usd REAL,
    reason TEXT, details_json TEXT
);

ALTER TABLE order_requests ADD COLUMN purpose TEXT CHECK (purpose IN ('entry','exit'));
ALTER TABLE order_requests ADD COLUMN position_id INTEGER REFERENCES positions(id);
ALTER TABLE order_requests ADD COLUMN evaluation_id TEXT;
ALTER TABLE order_requests ADD COLUMN reference_price REAL;
ALTER TABLE order_requests ADD COLUMN filled_ts TEXT;
ALTER TABLE order_requests ADD COLUMN avg_fill_price REAL;
ALTER TABLE order_requests ADD COLUMN reason TEXT;
ALTER TABLE order_requests ADD COLUMN external_order_id TEXT;
ALTER TABLE order_requests ADD COLUMN external_status TEXT;

ALTER TABLE positions ADD COLUMN signal_id INTEGER REFERENCES signals(id);
ALTER TABLE positions ADD COLUMN planned_risk_usd REAL;
ALTER TABLE positions ADD COLUMN initial_stop REAL;
ALTER TABLE positions ADD COLUMN mark_price REAL;
ALTER TABLE positions ADD COLUMN mark_ts TEXT;
ALTER TABLE positions ADD COLUMN last_managed_bar TEXT;
ALTER TABLE positions ADD COLUMN mfe_price REAL;
ALTER TABLE positions ADD COLUMN mae_price REAL;
ALTER TABLE positions ADD COLUMN r_multiple REAL;
ALTER TABLE positions ADD COLUMN trading_day TEXT;
-- DB-level guard: at most one open position per symbol (no pyramiding / averaging down)
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_open_position_per_symbol ON positions(symbol) WHERE status = 'open';
-- a closed position can never be re-opened
CREATE TRIGGER IF NOT EXISTS trg_no_reopen_position BEFORE UPDATE OF status ON positions
WHEN OLD.status = 'closed' AND NEW.status <> 'closed'
BEGIN SELECT RAISE(ABORT, 'closed positions cannot be re-opened'); END;

CREATE TRIGGER IF NOT EXISTS trg_no_delete_risk_evals BEFORE DELETE ON risk_evaluations
BEGIN SELECT RAISE(ABORT, 'audit table risk_evaluations is append-only'); END;
ALTER TABLE positions ADD COLUMN trading_day_closed TEXT;
