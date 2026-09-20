CREATE TABLE index_market_refresh_state (
    index_code TEXT PRIMARY KEY REFERENCES market_index_projection(code) ON DELETE CASCADE,
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('updated', 'failed')),
    source_id TEXT,
    error TEXT,
    CHECK ((outcome = 'updated' AND error IS NULL) OR outcome = 'failed')
);
