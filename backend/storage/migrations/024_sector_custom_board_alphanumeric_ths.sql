ALTER TABLE sector_custom_board RENAME TO sector_custom_board_old;
CREATE TABLE sector_custom_board (
    source_id TEXT NOT NULL,
    code TEXT NOT NULL CHECK (code GLOB '[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]'),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念', '指数')),
    added_at TEXT NOT NULL,
    ths_category TEXT CHECK (ths_category IS NULL OR ths_category IN ('industry', 'concept', 'index')),
    ths_page_code TEXT CHECK (ths_page_code IS NULL OR ths_page_code GLOB '[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]'),
    ths_market_code TEXT CHECK (ths_market_code IS NULL OR ths_market_code GLOB '[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]'),
    ths_prefix TEXT CHECK (ths_prefix IS NULL OR ths_prefix IN ('bk', 'zs', 'hs', '16')),
    history_source_id TEXT,
    history_source_code TEXT CHECK (history_source_code IS NULL OR history_source_code GLOB '[0-9A-Za-z][0-9A-Za-z]_[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]'),
    PRIMARY KEY (source_id, code)
);
INSERT INTO sector_custom_board
SELECT source_id,code,name,kind,added_at,ths_category,ths_page_code,ths_market_code,
       ths_prefix,history_source_id,history_source_code
FROM sector_custom_board_old;
DROP TABLE sector_custom_board_old;
