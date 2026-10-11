CREATE TABLE sector_removed_board (
    universe_type TEXT NOT NULL CHECK (universe_type IN ('hot_board', 'tracked_index')),
    source_id TEXT NOT NULL,
    code TEXT NOT NULL CHECK (code GLOB '[0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z][0-9A-Z]'),
    removed_at TEXT NOT NULL,
    PRIMARY KEY (universe_type, source_id, code)
);
