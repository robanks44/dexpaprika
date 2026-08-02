-- 0001_initial — ARCHITECTURE.md §3 schema v1.
-- Conventions: INTEGER PRIMARY KEY ids; ts/*_at TEXT ISO-8601 UTC; money/qty
-- TEXT Decimal strings (never REAL); portable DDL (Postgres/Timescale path:
-- time-series tables carry ts as the future hypertable partition column).
-- Constraint for the migrations runner: one statement per ';', no triggers.

CREATE TABLE providers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    rate_limit INTEGER NOT NULL,
    rate_period TEXT NOT NULL CHECK (rate_period IN ('second', 'minute', 'hour', 'day')),
    has_credits INTEGER NOT NULL DEFAULT 0 CHECK (has_credits IN (0, 1)),
    credit_limit INTEGER,
    free_tier INTEGER NOT NULL DEFAULT 1 CHECK (free_tier IN (0, 1)),
    config_json TEXT
);

CREATE TABLE provider_endpoint_costs (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    endpoint_pattern TEXT NOT NULL,
    credits INTEGER NOT NULL DEFAULT 1,
    UNIQUE (provider_id, endpoint_pattern)
);

CREATE TABLE api_call_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    endpoint TEXT NOT NULL,
    credits INTEGER NOT NULL DEFAULT 1,
    status INTEGER,
    latency_ms INTEGER,
    correlation_id TEXT
);

CREATE INDEX idx_api_call_log_provider_ts ON api_call_log (provider_id, ts DESC);

CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    chain TEXT NOT NULL,
    block_number INTEGER,
    kind TEXT NOT NULL,
    correlation_id TEXT
);

CREATE INDEX idx_snapshots_ts ON snapshots (ts DESC);

CREATE INDEX idx_snapshots_chain_ts ON snapshots (chain, ts DESC);

CREATE TABLE positions (
    id INTEGER PRIMARY KEY,
    wallet_ref TEXT NOT NULL,
    venue TEXT NOT NULL,
    chain TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('lp', 'perp', 'lend', 'borrow', 'holding', 'order')),
    external_id TEXT,
    group_tag TEXT NOT NULL CHECK (group_tag IN ('lp_hedge', 'defi', 'holdings')),
    opened_at TEXT,
    closed_at TEXT,
    metadata_json TEXT,
    UNIQUE (wallet_ref, venue, chain, kind, external_id)
);

CREATE TABLE position_events (
    id INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE RESTRICT,
    snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
    ts TEXT NOT NULL,
    type TEXT NOT NULL CHECK (
        type IN ('open', 'modify', 'partial_close', 'full_close', 'harvest', 'rebalance', 'observed')
    ),
    delta_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    tx_hash TEXT
);

CREATE INDEX idx_position_events_position_ts ON position_events (position_id, ts DESC);

CREATE INDEX idx_position_events_ts ON position_events (ts DESC);

CREATE TABLE hedge_state (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
    ts TEXT NOT NULL,
    lp_delta_eth TEXT NOT NULL,
    short_size_eth TEXT NOT NULL,
    coverage_ratio TEXT NOT NULL,
    pool_tick INTEGER NOT NULL,
    band_lower_tick INTEGER NOT NULL,
    band_upper_tick INTEGER NOT NULL,
    sl_trigger TEXT,
    distances_json TEXT
);

CREATE INDEX idx_hedge_state_ts ON hedge_state (ts DESC);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    venue TEXT NOT NULL,
    external_key TEXT NOT NULL,
    order_type INTEGER,
    trigger_price TEXT,
    size_delta TEXT,
    status TEXT NOT NULL,
    raw_json TEXT,
    UNIQUE (venue, external_key, ts)
);

CREATE INDEX idx_orders_ts ON orders (ts DESC);

CREATE TABLE alerts_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0 CHECK (delivered IN (0, 1)),
    ntfy_status TEXT
);

CREATE INDEX idx_alerts_log_ts ON alerts_log (ts DESC);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (
        phase IN ('intent', 'simulation', 'submission', 'confirmation', 'blocked', 'rejected')
    ),
    idempotency_key TEXT,
    payload_json TEXT NOT NULL
);

CREATE INDEX idx_audit_log_ts ON audit_log (ts DESC);

CREATE INDEX idx_audit_log_idempotency ON audit_log (idempotency_key);
