CREATE TABLE sector_custom_board_new (
    source_id TEXT NOT NULL,
    code TEXT NOT NULL CHECK (code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念', '指数')),
    added_at TEXT NOT NULL,
    ths_category TEXT CHECK (ths_category IS NULL OR ths_category IN ('industry', 'concept', 'index')),
    ths_page_code TEXT CHECK (ths_page_code IS NULL OR ths_page_code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'),
    ths_market_code TEXT CHECK (ths_market_code IS NULL OR ths_market_code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'),
    ths_prefix TEXT CHECK (ths_prefix IS NULL OR ths_prefix IN ('bk', 'zs')),
    history_source_id TEXT,
    history_source_code TEXT CHECK (history_source_code IS NULL OR history_source_code GLOB '[a-z][a-z]_[0-9][0-9][0-9][0-9][0-9][0-9]'),
    PRIMARY KEY (source_id, code)
);
INSERT INTO sector_custom_board_new(source_id,code,name,kind,added_at)
SELECT source_id,code,name,kind,added_at FROM sector_custom_board;
DROP TABLE sector_custom_board;
ALTER TABLE sector_custom_board_new RENAME TO sector_custom_board;
