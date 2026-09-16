-- Verification history, not inferred business-effective dates. Existing evidence is retained.
CREATE TABLE fund_relation_verification (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code TEXT NOT NULL REFERENCES fund_catalog_projection(code),
    checked_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('verified', 'unresolved', 'failed')),
    index_code TEXT,
    error TEXT,
    CHECK ((outcome = 'verified' AND index_code IS NOT NULL) OR outcome <> 'verified')
);
CREATE INDEX idx_relation_verification_latest ON fund_relation_verification(fund_code, id DESC);
