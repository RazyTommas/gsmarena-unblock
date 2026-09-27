BEGIN;

-- One serving projection for "what is this device running now", covering BOTH
-- firmware fact tables.
--
-- Why this exists. `v_latest_firmware` answers the question only for the
-- canonical layer, which on the live corpus is Samsung and nothing else:
-- firmware_releases holds 21,186 rows across 82 of 303 canonical devices, all
-- from samsung.fota. Every other brand's firmware lives in
-- product_firmware_releases (24,650 rows; Xiaomi 24,534, TECNO 80, itel 34,
-- Infinix 2), which had no latest-firmware view at all. The result reached the
-- user as a false statement: 156 canonical devices -- every Xiaomi, itel and
-- Infinix device and 40 TECNOs -- rendered as "Catalogued; firmware not
-- observed" while the corpus held observed firmware for them.
--
-- The two raw tables stay as they are. They are source-native, immutable and
-- genuinely different grains, and merging them would manufacture equivalence
-- between a vendor FOTA manifest and a community archive. What is unified here
-- is the SERVING CONTRACT: one row per (device, target, channel), carrying the
-- selection it made and the basis it made it on.
--
-- Both bases are recorded because they answer different questions and are not
-- interchangeable:
--   latest_basis       -- how we know this row is the latest one
--   effective_at_basis -- what effective_at actually measures
-- The second matters because firmware_releases.vendor_released_at is NOT a
-- vendor release date: every one of its 21,154 values is the first of a month,
-- derived from the build identifier, which the payload records as
-- date_basis='build_identifier_month_not_vendor_release'. A date that means
-- "the month named inside the build string" must never be rendered, sorted or
-- compared as though it meant "the day the vendor shipped it".

CREATE TABLE device_current_firmware (
  hardware_model_id           TEXT NOT NULL REFERENCES hardware_models(id),
  -- Region/CSC/target code. '' records that the source stated no target, which
  -- is not the same as a target whose code is unknown.
  target_key                  TEXT NOT NULL,
  channel                     TEXT NOT NULL,
  fact_layer                  TEXT NOT NULL CHECK (fact_layer IN ('canonical','evidence')),
  firmware_release_id         TEXT REFERENCES firmware_releases(id),
  product_firmware_release_id TEXT REFERENCES product_firmware_releases(id),
  source_id                   TEXT REFERENCES sources(id),
  build_id                    TEXT NOT NULL,
  android_version             TEXT,
  -- Major version as an integer, so the Android-ceiling filter is a numeric
  -- comparison rather than a string one. NULL where the source never stated a
  -- version: os_releases has 0 rows, so every canonical row is NULL today.
  android_major               INTEGER,
  security_patch_level        TEXT,
  -- Which publisher stated the patch level. It is frequently NOT the publisher
  -- that stated the build: samsung.fota manifests carry no patch level at all,
  -- so the SPL shown against a Samsung build comes from samsung.doc.aspl. The
  -- UI must be able to attribute it rather than implying one source said both.
  security_patch_level_source_id TEXT REFERENCES sources(id),
  effective_at                TEXT,
  effective_at_basis          TEXT NOT NULL CHECK (effective_at_basis IN (
                                'source_observed','vendor_stated_date',
                                'build_identifier_month','not_captured')),
  latest_basis                TEXT NOT NULL CHECK (latest_basis IN (
                                'source_manifest_latest','vendor_release_date',
                                'observation_order_only')),
  -- How many releases were observed in this partition, so the reader can tell a
  -- confident pick from a single sighting.
  release_count               INTEGER NOT NULL,
  PRIMARY KEY (hardware_model_id, target_key, channel),
  -- Exactly one side of the union, never both and never neither.
  CHECK ((firmware_release_id IS NULL) <> (product_firmware_release_id IS NULL))
) STRICT;

CREATE INDEX device_current_firmware_device_idx
  ON device_current_firmware(hardware_model_id, latest_basis, effective_at DESC);

-- Staging carries the same constraints so a malformed build fails here, before
-- anything is published, rather than being caught by a reader.
CREATE TABLE device_current_firmware_staging (
  hardware_model_id           TEXT NOT NULL,
  target_key                  TEXT NOT NULL,
  channel                     TEXT NOT NULL,
  fact_layer                  TEXT NOT NULL CHECK (fact_layer IN ('canonical','evidence')),
  firmware_release_id         TEXT,
  product_firmware_release_id TEXT,
  source_id                   TEXT,
  build_id                    TEXT NOT NULL,
  android_version             TEXT,
  android_major               INTEGER,
  security_patch_level        TEXT,
  security_patch_level_source_id TEXT,
  effective_at                TEXT,
  effective_at_basis          TEXT NOT NULL,
  latest_basis                TEXT NOT NULL,
  release_count               INTEGER NOT NULL,
  PRIMARY KEY (hardware_model_id, target_key, channel)
) STRICT;

-- Security patch levels, shredded out of observation payloads and keyed by the
-- build they describe.
--
-- firmware_releases.security_patch_level is NULL in all 21,186 rows, and that
-- is honest for its own source: a samsung.fota manifest states a build, a
-- baseband and a region, and never a patch level. The corpus does hold the
-- patch levels -- 15,738 samsung.doc.aspl observations covering 3,381 distinct
-- builds, of which 3,047 exact-match a firmware_releases.build_id -- but
-- nothing joined the two, so the UI's SPL column read "Unknown" for every
-- device and the Radar tile read "Security patches 0". A zero that means "we
-- never looked" is the most dangerous value a security tool can print.
--
-- build_id is a safe key here, verified rather than assumed: across all 15,738
-- observations, zero builds carry a conflicting aspl_date, a conflicting
-- patch_tier, or more than one model_code.
--
-- The source and observation are kept on the row because this is one
-- publisher's assertion about another publisher's build, and the difference has
-- to survive into the UI.
CREATE TABLE build_security_patch_levels (
  build_id             TEXT PRIMARY KEY,
  security_patch_level TEXT NOT NULL
    CHECK (security_patch_level GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  patch_tier           INTEGER,
  source_id            TEXT NOT NULL REFERENCES sources(id),
  observation_id       TEXT NOT NULL REFERENCES observations(id),
  built_at             TEXT NOT NULL
) STRICT;

-- Publication state. A reader that wants to know how old the projection is asks
-- here rather than inferring it from row contents.
CREATE TABLE projection_state (
  name          TEXT PRIMARY KEY,
  generation    INTEGER NOT NULL,
  built_at      TEXT NOT NULL,
  row_count     INTEGER NOT NULL,
  source_digest TEXT NOT NULL
) STRICT;

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(19,'device_current_firmware',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
