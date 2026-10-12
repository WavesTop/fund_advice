CREATE TABLE sector_custom_board_etf (
    source_id TEXT NOT NULL,
    code TEXT NOT NULL CHECK (code GLOB '[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]'),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念', '指数', 'ETF')),
    added_at TEXT NOT NULL,
    ths_category TEXT CHECK (ths_category IS NULL OR ths_category IN ('industry', 'concept', 'index', 'etf')),
    ths_page_code TEXT CHECK (ths_page_code IS NULL OR ths_page_code GLOB '[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]'),
    ths_market_code TEXT CHECK (ths_market_code IS NULL OR ths_market_code GLOB '[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]'),
    ths_prefix TEXT CHECK (ths_prefix IS NULL OR ths_prefix IN ('bk', 'zs', 'hs', '16', '17', '33')),
    history_source_id TEXT,
    history_source_code TEXT CHECK (history_source_code IS NULL OR history_source_code GLOB '[0-9A-Za-z][0-9A-Za-z]_[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]'),
    PRIMARY KEY (source_id, code)
);
INSERT INTO sector_custom_board_etf SELECT * FROM sector_custom_board;
DROP TABLE sector_custom_board;
ALTER TABLE sector_custom_board_etf RENAME TO sector_custom_board;

CREATE TABLE sector_heat_member_etf (
    code TEXT PRIMARY KEY,
    snapshot_id INTEGER NOT NULL DEFAULT 1 REFERENCES sector_heat_snapshot(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念', '指数', 'ETF')),
    heat_rank INTEGER NOT NULL UNIQUE,
    heat_value TEXT NOT NULL,
    heat_updated_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    updated_at TEXT,
    collection_error TEXT,
    rows_json TEXT NOT NULL,
    history_source_id TEXT,
    history_source_code TEXT,
    history_identity_match TEXT
);
INSERT INTO sector_heat_member_etf SELECT * FROM sector_heat_member;
DROP TABLE sector_heat_member;
ALTER TABLE sector_heat_member_etf RENAME TO sector_heat_member;

CREATE TABLE sector_identity_etf (
    source_id TEXT NOT NULL,
    source_code TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念', '指数', 'ETF')),
    taxonomy_id TEXT NOT NULL,
    taxonomy_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (source_id, source_code)
);
INSERT INTO sector_identity_etf SELECT * FROM sector_identity;
DROP TABLE sector_identity;
ALTER TABLE sector_identity_etf RENAME TO sector_identity;

CREATE TABLE sector_identity_revision_etf (
    source_id TEXT NOT NULL,
    source_code TEXT NOT NULL,
    taxonomy_id TEXT NOT NULL,
    taxonomy_revision TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念', '指数', 'ETF')),
    PRIMARY KEY (source_id, source_code, taxonomy_id, taxonomy_revision),
    FOREIGN KEY (taxonomy_id, taxonomy_revision)
        REFERENCES sector_taxonomy_revision(taxonomy_id, revision)
);
INSERT INTO sector_identity_revision_etf SELECT * FROM sector_identity_revision;
DROP TABLE sector_identity_revision;
ALTER TABLE sector_identity_revision_etf RENAME TO sector_identity_revision;
