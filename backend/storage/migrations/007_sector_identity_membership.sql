CREATE TABLE sector_identity (
    source_id TEXT NOT NULL,
    source_code TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念')),
    taxonomy_id TEXT NOT NULL,
    taxonomy_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (source_id, source_code)
);

CREATE TABLE sector_membership_snapshot (
    board_source_id TEXT NOT NULL,
    board_code TEXT NOT NULL,
    as_of TEXT NOT NULL CHECK (as_of GLOB '????-??-??'),
    fetched_at TEXT,
    member_count INTEGER NOT NULL DEFAULT 0 CHECK (member_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('ready', 'failed')),
    error TEXT,
    PRIMARY KEY (board_source_id, board_code, as_of)
);

CREATE TABLE sector_membership (
    board_source_id TEXT NOT NULL,
    board_code TEXT NOT NULL,
    as_of TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    market INTEGER NOT NULL,
    source_order INTEGER NOT NULL CHECK (source_order > 0),
    market_cap TEXT,
    weight TEXT,
    PRIMARY KEY (board_source_id, board_code, as_of, stock_code),
    FOREIGN KEY (board_source_id, board_code, as_of)
        REFERENCES sector_membership_snapshot(board_source_id, board_code, as_of)
        ON DELETE CASCADE
);

CREATE INDEX idx_sector_membership_stock
    ON sector_membership(stock_code, board_source_id, board_code, as_of);
