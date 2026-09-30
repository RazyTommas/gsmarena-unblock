BEGIN;

-- An FTS5 trigram index that NARROWS the release search. LIKE still decides it.
--
-- /api/v1/search asks three questions and releases is almost all of the cost.
-- Measured on the live corpus, warm, medians of 5:
--
--     query      devices    chips  releases   total
--     q=_           0.7ms   11.5ms    50.4ms  62.1ms
--     q=5G          0.7ms   11.9ms    59.3ms  70.2ms
--     q=Galaxy      0.6ms   12.2ms    71.2ms  80.2ms
--
-- The 50ms is not the LIKE evaluation -- `q=%` matches nothing and still costs
-- 51ms. It is scanning 21,186 firmware releases through the four joins behind
-- v_device_region_history. An index that proposes candidates lets that join run
-- over the candidates instead of the catalogue.
--
-- WHY TRIGRAM AND NOT THE DEFAULT TOKENIZER
-- The searchable values are build ids and model codes -- `S918BXXSAFZH3`,
-- `SM-S938B` -- and readers search the middle of them. A word tokenizer cannot
-- match mid-token, so it would change which releases a reader finds. `trigram`
-- is the FTS5 tokenizer built for substring matching. `detail=full` is required
-- for phrase matching, which is what makes a trigram sequence mean "in this
-- order" rather than "these three-character groups occur somewhere".
--
-- WHY release_id IS UNINDEXED
-- It is the key the outer query joins back on, never something anyone searches
-- for by substring. Indexing it would put every firmware release id's trigrams
-- in the index and let `MATCH` propose candidates because their ID looked like
-- the query.
--
-- This creates the index EMPTY. src/mobile_observatory/search_index.py fills it
-- from the batch, and integrity.check_corpus reports it as an error while it
-- does not match the corpus -- so an index created here and never built reads as
-- stale, which it is, rather than as an index that finds nothing.
CREATE VIRTUAL TABLE firmware_release_search USING fts5(
  release_id UNINDEXED,
  body,
  tokenize='trigram',
  detail=full
);

-- What the index was built FROM, so "is it stale" is answerable.
--
-- Two columns for two different readers, because they can afford different
-- amounts of work and saying so is the point:
--
--   basis_digest  the whole indexed text, digested. Catches any change,
--                 including an edit that leaves the row count alone. Costs a
--                 66ms re-derivation, so check_corpus checks it once per batch.
--   release_rows  count(*) on firmware_releases. Costs 0.01ms, so the request
--                 path checks it on every search and falls back to the scan
--                 when it does not match. Catches rows arriving or leaving,
--                 which is the staleness that actually happens here, and NOT a
--                 same-count edit.
--
-- A stale index that looks fresh is worse than no index, so neither reader
-- trusts the other's check: the cheap one cannot conclude "fresh", only "not
-- obviously stale", and the query path treats anything else as a reason to scan.
CREATE TABLE search_index_state (
  name          TEXT PRIMARY KEY,
  source_rows   INTEGER NOT NULL,
  release_rows  INTEGER NOT NULL,
  basis_digest  TEXT NOT NULL,
  built_at      TEXT NOT NULL
) STRICT;

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(32,'firmware_release_search',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
