PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- Retiring a superseded observation (dedupe.py's retire_observations, used both by
-- IngestionImporter on every reimport and by the one-off dedupe_reingested_observations
-- cleanup) walks evidence by observation_id and walks every evidence-referencing table
-- by evidence_id. None of the three had an index on that lookup column: evidence's own
-- UNIQUE(artifact_id, observation_id, locator) index is keyed by artifact_id first, so
-- it does not serve a "WHERE observation_id=?" lookup, and firmware_release_evidence /
-- domain_events had no index on evidence_id at all. Retiring the 21,186 duplicate
-- samsung.fota observations measured 2026-09 without these was a full scan of each
-- table per row retired -- these make it proportional to the work instead.
CREATE INDEX evidence_observation_idx ON evidence(observation_id);
CREATE INDEX firmware_release_evidence_evidence_idx ON firmware_release_evidence(evidence_id);
CREATE INDEX domain_events_evidence_idx ON domain_events(evidence_id) WHERE evidence_id IS NOT NULL;

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(17,'evidence_retire_indexes',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
