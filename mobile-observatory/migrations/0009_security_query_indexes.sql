-- Read-path indexes only. Facts and confidence boundaries are unchanged.
BEGIN IMMEDIATE;
CREATE INDEX applicability_vulnerability_idx ON applicability_claims(vulnerability_id,relationship,subject_type,subject_id);
CREATE INDEX fix_vulnerability_idx ON fix_claims(vulnerability_id);
CREATE INDEX advisory_vulnerability_reverse_idx ON advisory_vulnerabilities(vulnerability_id,advisory_id);
INSERT INTO schema_migrations(version,name,applied_at)
VALUES(9,'security_query_indexes',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
