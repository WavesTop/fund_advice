CREATE TABLE IF NOT EXISTS fund_timeseries_collection_run (
    run_id TEXT PRIMARY KEY,
    start_date TEXT NOT NULL CHECK (start_date GLOB '????????'),
    end_date TEXT NOT NULL CHECK (end_date GLOB '????????'),
    registry_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS fund_timeseries_collection_item (
    run_id TEXT NOT NULL REFERENCES fund_timeseries_collection_run(run_id) ON DELETE CASCADE,
    code TEXT NOT NULL REFERENCES fund_catalog_projection(code),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error_code TEXT,
    last_error_message TEXT,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (run_id, code)
);

CREATE INDEX IF NOT EXISTS idx_fund_timeseries_collection_work
    ON fund_timeseries_collection_item(run_id, status, code);
