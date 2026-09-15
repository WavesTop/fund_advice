CREATE TABLE IF NOT EXISTS market_index_projection (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fund_market_relation (
    fund_code TEXT NOT NULL REFERENCES fund_catalog_projection(code),
    index_code TEXT NOT NULL REFERENCES market_index_projection(code),
    relation_type TEXT NOT NULL CHECK (relation_type IN ('tracked_index', 'sector_index')),
    source_id TEXT NOT NULL,
    evidence_url TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    PRIMARY KEY (fund_code, index_code, relation_type)
);

CREATE TABLE IF NOT EXISTS market_index_daily (
    index_code TEXT NOT NULL REFERENCES market_index_projection(code) ON DELETE CASCADE,
    date TEXT NOT NULL CHECK (date GLOB '????-??-??'),
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT,
    amount TEXT,
    PRIMARY KEY (index_code, date)
);
