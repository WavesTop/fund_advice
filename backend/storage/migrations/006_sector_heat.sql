CREATE TABLE sector_heat_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    as_of TEXT,
    updated_at TEXT,
    requested_count INTEGER NOT NULL DEFAULT 100,
    catalog_count INTEGER NOT NULL DEFAULT 0,
    collected_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT NOT NULL,
    last_error TEXT
);

CREATE TABLE sector_heat_member (
    code TEXT PRIMARY KEY,
    snapshot_id INTEGER NOT NULL DEFAULT 1 REFERENCES sector_heat_snapshot(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念')),
    heat_rank INTEGER NOT NULL UNIQUE,
    heat_value TEXT NOT NULL,
    heat_updated_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    updated_at TEXT,
    collection_error TEXT,
    rows_json TEXT NOT NULL
);
