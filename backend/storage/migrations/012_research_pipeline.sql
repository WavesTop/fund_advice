-- Additive research storage. Existing market projections and audit history are untouched.
CREATE TABLE raw_asset (
    asset_id TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    media_type TEXT NOT NULL,
    body BLOB NOT NULL,
    body_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE data_revision (
    revision_id TEXT PRIMARY KEY,
    dataset_kind TEXT NOT NULL CHECK(dataset_kind IN ('numeric','series','fund_profile')),
    record_key TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    valid_to TEXT,
    published_at TEXT,
    publication_precision TEXT NOT NULL CHECK(publication_precision IN ('time','day','unknown')),
    first_seen_at TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    raw_asset_id TEXT NOT NULL REFERENCES raw_asset(asset_id),
    parser_version TEXT NOT NULL,
    supersedes_revision_id TEXT REFERENCES data_revision(revision_id),
    CHECK(valid_to IS NULL OR valid_to > effective_date)
);
CREATE INDEX data_revision_lookup ON data_revision(record_key,source_id,committed_at);
CREATE INDEX data_revision_subject_time ON data_revision(subject_key,effective_date,committed_at);
CREATE TABLE numeric_fact (
    revision_id TEXT PRIMARY KEY REFERENCES data_revision(revision_id),
    metric TEXT NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    scope TEXT NOT NULL,
    methodology TEXT NOT NULL,
    semantic_status TEXT NOT NULL CHECK(semantic_status IN ('verified','background','unverified'))
);
CREATE TABLE series_fact (
    revision_id TEXT PRIMARY KEY REFERENCES data_revision(revision_id),
    series_kind TEXT NOT NULL CHECK(series_kind IN ('price','nav','total_return')),
    value TEXT NOT NULL,
    amount TEXT,
    currency TEXT NOT NULL,
    basis TEXT NOT NULL,
    semantic_status TEXT NOT NULL CHECK(semantic_status IN ('verified','unverified'))
);
CREATE TABLE research_fund_profile (
    revision_id TEXT PRIMARY KEY REFERENCES data_revision(revision_id),
    fund_code TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    benchmark_key TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    annual_fee_bps TEXT,
    subscription_status TEXT NOT NULL,
    semantic_status TEXT NOT NULL CHECK(semantic_status IN ('verified','unverified'))
);
CREATE TABLE data_observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id TEXT NOT NULL REFERENCES data_revision(revision_id),
    raw_asset_id TEXT NOT NULL REFERENCES raw_asset(asset_id),
    fetched_at TEXT NOT NULL
);
CREATE TABLE data_quality_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id TEXT NOT NULL REFERENCES data_revision(revision_id),
    occurred_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('available','quarantined')),
    reason TEXT NOT NULL
);
CREATE TABLE data_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    selection_mode TEXT NOT NULL CHECK(selection_mode='system_as_of'),
    cutoff TEXT NOT NULL,
    created_at TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL
);
CREATE TABLE snapshot_item (
    snapshot_id TEXT NOT NULL REFERENCES data_snapshot(snapshot_id),
    revision_id TEXT NOT NULL REFERENCES data_revision(revision_id),
    PRIMARY KEY(snapshot_id,revision_id)
);
CREATE TABLE analysis_run (
    run_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES data_snapshot(snapshot_id),
    algorithm_version TEXT NOT NULL,
    implementation_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    output_json TEXT NOT NULL
);
CREATE TABLE collection_run (
    run_id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state TEXT NOT NULL CHECK(state IN ('running','success','partial','failed','timeout','interrupted')),
    owner_pid INTEGER NOT NULL,
    result_json TEXT
);
CREATE UNIQUE INDEX one_running_collection ON collection_run(target) WHERE state='running';
CREATE TABLE collection_attempt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES collection_run(run_id),
    occurred_at TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    url TEXT NOT NULL,
    http_status INTEGER,
    error TEXT,
    raw_asset_id TEXT REFERENCES raw_asset(asset_id)
);
CREATE TABLE validation_trial (
    trial_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    protocol_hash TEXT NOT NULL,
    protocol_json TEXT NOT NULL
);
CREATE TABLE forward_observation (
    observation_id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL REFERENCES validation_trial(trial_id),
    run_id TEXT NOT NULL REFERENCES analysis_run(run_id),
    created_at TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    benchmark_key TEXT NOT NULL,
    horizon INTEGER NOT NULL CHECK(horizon>0),
    entry_date TEXT NOT NULL,
    UNIQUE(trial_id,run_id,subject_key,benchmark_key,horizon)
);
CREATE TABLE validation_report (
    report_id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL REFERENCES validation_trial(trial_id),
    created_at TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TRIGGER immutable_raw_asset_update BEFORE UPDATE ON raw_asset
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_raw_asset_delete BEFORE DELETE ON raw_asset
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_data_revision_update BEFORE UPDATE ON data_revision
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_data_revision_delete BEFORE DELETE ON data_revision
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_numeric_fact_update BEFORE UPDATE ON numeric_fact
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_numeric_fact_delete BEFORE DELETE ON numeric_fact
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_series_fact_update BEFORE UPDATE ON series_fact
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_series_fact_delete BEFORE DELETE ON series_fact
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_research_fund_profile_update BEFORE UPDATE ON research_fund_profile
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_research_fund_profile_delete BEFORE DELETE ON research_fund_profile
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_data_observation_update BEFORE UPDATE ON data_observation
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_data_observation_delete BEFORE DELETE ON data_observation
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_data_quality_event_update BEFORE UPDATE ON data_quality_event
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_data_quality_event_delete BEFORE DELETE ON data_quality_event
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_data_snapshot_update BEFORE UPDATE ON data_snapshot
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_data_snapshot_delete BEFORE DELETE ON data_snapshot
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_snapshot_item_update BEFORE UPDATE ON snapshot_item
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_snapshot_item_delete BEFORE DELETE ON snapshot_item
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_analysis_run_update BEFORE UPDATE ON analysis_run
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_analysis_run_delete BEFORE DELETE ON analysis_run
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_collection_attempt_update BEFORE UPDATE ON collection_attempt
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_collection_attempt_delete BEFORE DELETE ON collection_attempt
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_validation_trial_update BEFORE UPDATE ON validation_trial
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_validation_trial_delete BEFORE DELETE ON validation_trial
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_forward_observation_update BEFORE UPDATE ON forward_observation
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_forward_observation_delete BEFORE DELETE ON forward_observation
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_validation_report_update BEFORE UPDATE ON validation_report
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
CREATE TRIGGER immutable_validation_report_delete BEFORE DELETE ON validation_report
BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
