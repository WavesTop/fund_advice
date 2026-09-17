-- Source-exact observations, including unsuccessful collection attempts.
CREATE TABLE sector_fundamental_observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX idx_sector_fundamental_asof
ON sector_fundamental_observation(subject_key, observed_at, id);
CREATE TRIGGER sector_fundamental_no_update BEFORE UPDATE ON sector_fundamental_observation
BEGIN
    SELECT RAISE(ABORT, 'sector observations are immutable');
END;
CREATE TRIGGER sector_fundamental_no_delete BEFORE DELETE ON sector_fundamental_observation
BEGIN
    SELECT RAISE(ABORT, 'sector observations are immutable');
END;
