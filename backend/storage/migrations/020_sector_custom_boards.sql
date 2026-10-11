CREATE TABLE sector_custom_board (
    source_id TEXT NOT NULL,
    code TEXT NOT NULL CHECK (code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念')),
    added_at TEXT NOT NULL,
    PRIMARY KEY (source_id, code)
);
