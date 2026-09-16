PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
-- Source assertions are not canonical hardware mappings or support assertions.
CREATE TABLE source_specifications (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  source_key TEXT NOT NULL,
  product_id TEXT REFERENCES source_products(id),
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
  manufacturer TEXT NOT NULL,
  name TEXT NOT NULL,
  codename TEXT NOT NULL,
  chipset TEXT NOT NULL,
  models_json TEXT NOT NULL CHECK(json_valid(models_json)),
  source_url TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  match_method TEXT NOT NULL,
  UNIQUE(source_id,source_key,evidence_id)
) STRICT;
CREATE INDEX source_specifications_product_idx ON source_specifications(product_id);
INSERT INTO schema_migrations(version,name,applied_at)
VALUES(10,'source_specifications',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
