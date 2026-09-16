PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE source_products (
  id TEXT PRIMARY KEY,
  manufacturer TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  review_state TEXT NOT NULL CHECK(review_state IN ('proposed','approved','rejected')) DEFAULT 'proposed',
  specification_json TEXT CHECK(specification_json IS NULL OR json_valid(specification_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(manufacturer, normalized_name)
) STRICT;

CREATE TABLE source_identity_registry (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  namespace TEXT NOT NULL,
  source_value TEXT NOT NULL,
  normalized_value TEXT NOT NULL,
  product_id TEXT REFERENCES source_products(id),
  resolution_state TEXT NOT NULL CHECK(resolution_state IN ('proposed','approved','rejected','ambiguous')),
  resolution_method TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  confidence TEXT NOT NULL CHECK(confidence IN ('authoritative','high','medium','low')),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE(source_id, namespace, normalized_value)
) STRICT;

CREATE TABLE observation_product_links (
  observation_id TEXT PRIMARY KEY REFERENCES observations(id),
  product_id TEXT NOT NULL REFERENCES source_products(id),
  identity_id TEXT NOT NULL REFERENCES source_identity_registry(id),
  link_state TEXT NOT NULL CHECK(link_state IN ('proposed','approved')),
  created_at TEXT NOT NULL
) STRICT;

CREATE INDEX source_products_review_idx ON source_products(review_state, manufacturer, canonical_name);
CREATE INDEX identity_product_idx ON source_identity_registry(product_id, resolution_state);

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(3,'identity_registry',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
