-- 0003_recorder — S12a: recorder-service liveness heartbeat.
-- Append-only per-cycle stamp; readers never block the writer (WAL). ts TEXT
-- ISO-8601 UTC; one statement per ';'. No change to existing tables — the full
-- raw variable set stays in position_events.state_json / hedge_state /
-- orders.raw_json (ENGINEERING_STANDARDS §2: source + as_of on every datapoint).

CREATE TABLE recorder_heartbeat (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    ok INTEGER NOT NULL,
    block INTEGER,
    detail_json TEXT
);

CREATE INDEX idx_recorder_heartbeat_kind_ts ON recorder_heartbeat (kind, ts DESC);
