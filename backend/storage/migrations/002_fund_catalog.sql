CREATE TABLE IF NOT EXISTS fund_catalog_import_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    committed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS fund_catalog_projection (
    share_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE CHECK (length(code) = 6 AND code GLOB '[0-9]*'),
    name TEXT NOT NULL,
    fund_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    first_seen_batch_id INTEGER NOT NULL REFERENCES fund_catalog_import_batch(id),
    last_seen_batch_id INTEGER NOT NULL REFERENCES fund_catalog_import_batch(id),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_fund_catalog_name ON fund_catalog_projection(name);
CREATE INDEX IF NOT EXISTS idx_fund_catalog_type ON fund_catalog_projection(fund_type);
