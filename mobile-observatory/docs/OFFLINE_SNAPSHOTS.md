# Offline snapshots

An offline bundle is a self-describing directory containing:

- `corpus.sqlite`: a consistent SQLite backup of canonical and ingestion data;
- `local.sqlite`: separate user-owned preferences, watches, saved searches, and seen state;
- `web/`: the static application assets;
- `manifest.json`: bundle/schema/API versions, snapshot time, stable snapshot ID,
  file sizes and SHA-256 hashes, plus a row count for every corpus table.

`SnapshotBuilder` copies the database through SQLite's backup API, constructs the
bundle in a sibling temporary directory, verifies every hash, both database integrity
checks, table counts, and the web entry point, then atomically renames it into place.
It refuses to overwrite an existing bundle. `corpus.sqlite` is published read-only;
`local.sqlite` remains writable and is explicitly marked for preservation on upgrade.

```python
from pathlib import Path
from mobile_observatory.snapshots import SnapshotBuilder, verify_bundle

bundle = SnapshotBuilder(
    corpus=Path("var/mobile-observatory.sqlite"),
    web_assets=Path("apps/web"),
    api_version="v1",
).build(Path("dist/mobile-observatory-2026-09-16"))

verify_bundle(bundle)
```

Copy or archive only a bundle that passes `verify_bundle`. An offline UI must display
`snapshot_at` and source freshness from the corpus; it must not imply live freshness.
