ALTER TABLE sector_heat_member RENAME TO sector_heat_member_old;
CREATE TABLE sector_heat_member (
    code TEXT PRIMARY KEY,
    snapshot_id INTEGER NOT NULL DEFAULT 1 REFERENCES sector_heat_snapshot(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念', '指数')),
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
INSERT INTO sector_heat_member
SELECT code,snapshot_id,name,kind,heat_rank,heat_value,heat_updated_at,source_id,updated_at,
       collection_error,rows_json,history_source_id,history_source_code,history_identity_match
FROM sector_heat_member_old;
DROP TABLE sector_heat_member_old;

ALTER TABLE sector_identity RENAME TO sector_identity_old;
CREATE TABLE sector_identity (
    source_id TEXT NOT NULL,
    source_code TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念', '指数')),
    taxonomy_id TEXT NOT NULL,
    taxonomy_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (source_id, source_code)
);
INSERT INTO sector_identity
SELECT source_id,source_code,name,kind,taxonomy_id,taxonomy_revision,observed_at
FROM sector_identity_old;
DROP TABLE sector_identity_old;

ALTER TABLE sector_identity_revision RENAME TO sector_identity_revision_old;
CREATE TABLE sector_identity_revision (
    source_id TEXT NOT NULL,
    source_code TEXT NOT NULL,
    taxonomy_id TEXT NOT NULL,
    taxonomy_revision TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念', '指数')),
    PRIMARY KEY (source_id, source_code, taxonomy_id, taxonomy_revision),
    FOREIGN KEY (taxonomy_id, taxonomy_revision)
        REFERENCES sector_taxonomy_revision(taxonomy_id, revision)
);
INSERT INTO sector_identity_revision
SELECT source_id,source_code,taxonomy_id,taxonomy_revision,name,kind
FROM sector_identity_revision_old;
DROP TABLE sector_identity_revision_old;
