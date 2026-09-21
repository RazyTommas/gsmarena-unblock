PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE source_data_corrections (
 id TEXT PRIMARY KEY,
 entity_type TEXT NOT NULL,
 entity_id TEXT NOT NULL,
 reason TEXT NOT NULL,
 before_json TEXT NOT NULL CHECK(json_valid(before_json)),
 after_json TEXT NOT NULL CHECK(json_valid(after_json)),
 evidence_id TEXT REFERENCES evidence(id),
 recorded_at TEXT NOT NULL,
 UNIQUE(entity_type,entity_id,reason)
) STRICT;
CREATE INDEX source_data_corrections_entity_idx ON source_data_corrections(entity_type,entity_id);
CREATE TRIGGER source_data_corrections_no_update BEFORE UPDATE ON source_data_corrections
 BEGIN SELECT RAISE(ABORT,'source corrections are immutable'); END;
CREATE TRIGGER source_data_corrections_no_delete BEFORE DELETE ON source_data_corrections
 BEGIN SELECT RAISE(ABORT,'source corrections are immutable'); END;
CREATE INDEX domain_events_corrections_idx ON domain_events(corrects_event_id) WHERE corrects_event_id IS NOT NULL;
CREATE VIEW v_current_domain_events AS
 SELECT original.* FROM domain_events original
 WHERE NOT EXISTS(SELECT 1 FROM domain_events correction WHERE correction.corrects_event_id=original.id);
INSERT INTO schema_migrations(version,name,applied_at)
VALUES(12,'source_data_corrections',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
