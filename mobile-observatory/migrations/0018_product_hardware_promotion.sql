-- 0017_product_hardware_promotion.sql
--
-- Bridges an approved source_product to the canonical hardware_model it was
-- promoted to. Before this migration, repository.py::create_device() was only
-- ever called from two hardcoded Samsung seed paths in batch.py -- there was no
-- table recording "this product became that device", so there was also no way
-- to promote any other manufacturer idempotently or to tell a genuine promotion
-- apart from a duplicate.
--
-- product_id is the PRIMARY KEY: a product can be promoted at most once, so
-- re-running the promotion step is a no-op for products it already handled
-- (idempotent). hardware_model_id is UNIQUE: two different products can never
-- both claim the same device, so a second product trying to land on an
-- already-claimed device fails the constraint instead of silently merging.
--
-- hardware_models.model_code is NOT NULL by design (0001_canonical_core.sql).
-- Most source_products carry no genuine per-unit hardware code at all -- GSMArena's
-- captured specs carry a device NAME and a page slug, never a factory model
-- code, and Xiaomi's captured "model_code" field is an internal build codename,
-- not a retail model number (see collectors/adapters/xiaomi_tracker.py). A
-- product can therefore only be promoted when either (a) it names the same
-- device an existing hardware_model already represents, or (b) a genuine model
-- code was actually observed (e.g. Google Play's authoritative supported-devices
-- "Model" column) -- never fabricated to satisfy the NOT NULL constraint.
PRAGMA foreign_keys=ON;
BEGIN IMMEDIATE;

CREATE TABLE product_hardware_links (
  product_id TEXT PRIMARY KEY REFERENCES source_products(id),
  hardware_model_id TEXT NOT NULL UNIQUE REFERENCES hardware_models(id),
  model_code_source TEXT NOT NULL,
  evidence_id TEXT REFERENCES evidence(id),
  created_at TEXT NOT NULL
) STRICT;

CREATE INDEX product_hardware_links_model_idx ON product_hardware_links(hardware_model_id);

-- Makes a promoted device's firmware reachable from hardware_models without
-- duplicating rows into firmware_releases: firmware_releases.channel is a
-- closed vocabulary ('stable','beta','preview','internal','unknown') tied to
-- vendor firmware semantics, while product_firmware_releases.channel is a raw
-- source branch label (e.g. a Xiaomi ROM branch name). Reconciling those is a
-- bigger change than this promotion step; a nullable back-reference is the
-- trivial way to make the link navigable today.
ALTER TABLE product_firmware_releases ADD COLUMN hardware_model_id TEXT REFERENCES hardware_models(id);
CREATE INDEX product_firmware_releases_hw_idx ON product_firmware_releases(hardware_model_id);

-- Renumbered 0017 -> 0018. Two agents authored a migration 0017 in parallel
-- (evidence_retire_indexes and this one). database.py keys applied migrations by
-- int(filename.split('_')[0]) into a SET, so two files sharing a number means a
-- corpus that recorded 17 from one of them would NEVER apply the other.
INSERT INTO schema_migrations(version,name,applied_at)
VALUES(18,'product_hardware_promotion',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
