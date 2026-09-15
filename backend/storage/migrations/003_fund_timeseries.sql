CREATE TABLE IF NOT EXISTS fund_timeseries_import_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL REFERENCES fund_catalog_projection(code),
    kind TEXT NOT NULL CHECK (kind IN ('price', 'nav')),
    source_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count > 0),
    committed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS fund_timeseries_projection (
    code TEXT NOT NULL REFERENCES fund_catalog_projection(code),
    kind TEXT NOT NULL CHECK (kind IN ('price', 'nav')),
    source_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES fund_timeseries_import_batch(id),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (code, kind)
);

CREATE TABLE IF NOT EXISTS fund_price_daily (
    code TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'price' CHECK (kind = 'price'),
    date TEXT NOT NULL CHECK (date GLOB '????-??-??'),
    open TEXT,
    high TEXT,
    low TEXT,
    close TEXT,
    volume TEXT,
    amount TEXT,
    PRIMARY KEY (code, date),
    FOREIGN KEY (code, kind) REFERENCES fund_timeseries_projection(code, kind) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS fund_nav_daily (
    code TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'nav' CHECK (kind = 'nav'),
    date TEXT NOT NULL CHECK (date GLOB '????-??-??'),
    unit_nav TEXT,
    accumulated_nav TEXT,
    PRIMARY KEY (code, date),
    FOREIGN KEY (code, kind) REFERENCES fund_timeseries_projection(code, kind) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fund_timeseries_batch ON fund_timeseries_import_batch(code, kind, id DESC);
