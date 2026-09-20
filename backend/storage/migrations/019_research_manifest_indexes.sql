-- Speed up point-in-time research manifests used by sector opportunities.
CREATE INDEX IF NOT EXISTS idx_data_observation_revision_time
ON data_observation(revision_id, fetched_at);

CREATE INDEX IF NOT EXISTS idx_data_quality_event_revision_time
ON data_quality_event(revision_id, occurred_at, id);
