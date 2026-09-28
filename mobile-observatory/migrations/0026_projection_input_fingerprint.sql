BEGIN;

-- Detect a stale projection by what it was BUILT FROM, not by the clock.
--
-- The staleness check compared max(observations.observed_at) against the
-- projection's built_at. That only catches evidence whose OBSERVATION TIME is
-- newer than the build, which misses most of the ways the read path actually
-- goes stale:
--   * a replay imported today carrying last week's observation times reads as
--     healthy while its firmware is absent from the projection;
--   * a promotion that creates devices adds no observation at all;
--   * a correction to a date, a region or an identity name adds none either;
--   * changing a source's currency_rank changes which build every affected
--     device is said to be running, and touches no observation.
--
-- This records a fingerprint of the INPUT tables at build time. The check then
-- asks the question that matters -- "would building again produce something
-- different?" -- instead of comparing two timestamps that can both be old.
ALTER TABLE projection_state ADD COLUMN input_fingerprint TEXT NOT NULL DEFAULT '';

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(26,'projection_input_fingerprint',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
