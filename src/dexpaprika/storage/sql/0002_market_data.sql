-- 0002_market_data — S3: DexPaprika market history recording.
-- Money/prices TEXT Decimal strings; ts/as_of TEXT ISO-8601 UTC; source on
-- every datapoint (ENGINEERING_STANDARDS §2). One statement per ';'.

CREATE TABLE pool_metrics (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    network TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    source TEXT NOT NULL,
    price_usd TEXT,
    liquidity_usd TEXT,
    volume_24h_usd TEXT,
    txns_24h INTEGER,
    fee TEXT,
    raw_json TEXT
);

CREATE INDEX idx_pool_metrics_pool_ts ON pool_metrics (network, pool_address, ts DESC);

CREATE TABLE ohlcv (
    id INTEGER PRIMARY KEY,
    network TEXT NOT NULL,
    pool_address TEXT NOT NULL,
    interval TEXT NOT NULL,
    ts_start TEXT NOT NULL,
    ts_end TEXT,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT,
    source TEXT NOT NULL,
    as_of TEXT NOT NULL,
    UNIQUE (network, pool_address, interval, ts_start)
);

CREATE INDEX idx_ohlcv_pool_ts ON ohlcv (network, pool_address, interval, ts_start DESC);
