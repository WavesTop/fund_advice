CREATE TABLE sector_taxonomy_revision (
    taxonomy_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    provider TEXT NOT NULL,
    as_of TEXT NOT NULL CHECK (as_of GLOB '????-??-??'),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (taxonomy_id, revision)
);

CREATE TABLE sector_identity_revision (
    source_id TEXT NOT NULL,
    source_code TEXT NOT NULL,
    taxonomy_id TEXT NOT NULL,
    taxonomy_revision TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('行业', '概念')),
    PRIMARY KEY (source_id, source_code, taxonomy_id, taxonomy_revision),
    FOREIGN KEY (taxonomy_id, taxonomy_revision)
        REFERENCES sector_taxonomy_revision(taxonomy_id, revision)
);
