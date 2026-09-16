PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE product_firmware_releases (
  id TEXT PRIMARY KEY,
  product_id TEXT NOT NULL REFERENCES source_products(id),
  identity_id TEXT NOT NULL REFERENCES source_identity_registry(id),
  observation_id TEXT NOT NULL UNIQUE REFERENCES observations(id),
  source_id TEXT NOT NULL REFERENCES sources(id),
  region_code TEXT NOT NULL,
  build_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  android_version TEXT,
  android_major INTEGER,
  vendor_released_at TEXT,
  delivery_method TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(product_id, identity_id, build_id, channel, delivery_method)
) STRICT;

CREATE INDEX product_firmware_history_idx
ON product_firmware_releases(product_id, region_code, channel,
                             vendor_released_at DESC, id);

CREATE TABLE product_security_publications (
  id TEXT PRIMARY KEY,
  product_id TEXT NOT NULL REFERENCES source_products(id),
  identity_id TEXT NOT NULL REFERENCES source_identity_registry(id),
  observation_id TEXT NOT NULL UNIQUE REFERENCES observations(id),
  source_id TEXT NOT NULL REFERENCES sources(id),
  security_patch_month TEXT NOT NULL,
  published_at TEXT,
  title TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(product_id, security_patch_month, observation_id)
) STRICT;

CREATE INDEX product_security_history_idx
ON product_security_publications(product_id, security_patch_month DESC);

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(7,'product_release_views',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
