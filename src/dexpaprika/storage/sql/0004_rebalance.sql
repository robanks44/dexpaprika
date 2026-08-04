-- 0004_rebalance — S14: delta-band rebalance decision journal (net-capital attribution).
-- Append-only. Records EVERY decision (shadow + executed) with enough to score realized
-- net-capital impact from the recorded time-series later. Money as TEXT Decimal strings;
-- ts TEXT ISO-8601 UTC; one statement per ';'.

CREATE TABLE rebalance_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    decision TEXT NOT NULL,
    current_eth TEXT,
    target_eth TEXT,
    deviation TEXT,
    band TEXT,
    price_usd TEXT,
    est_move_usd TEXT,
    executed INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT,
    snapshot_id INTEGER,
    gates_json TEXT,
    thesis TEXT,
    outcome_json TEXT
);

CREATE INDEX idx_rebalance_log_ts ON rebalance_log (ts DESC);
CREATE INDEX idx_rebalance_log_executed_ts ON rebalance_log (executed, ts DESC);
