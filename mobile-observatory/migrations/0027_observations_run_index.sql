BEGIN;

-- "Which observations does this run hold?" had no index.
--
-- integrity.check_corpus asks it once per ingestion run, and every ask was a
-- full SCAN of 96,319 rows: 322ms for a question about 21 runs. The same
-- lookup is what batch reporting and any per-run audit need.
CREATE INDEX observations_run_idx ON observations(run_id);

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(27,'observations_run_index',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
