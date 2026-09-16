# Portable reviewed snapshot — 2026-09-17

Clone this repository or use GitHub's **Code → Download ZIP**, then unzip it.
Python 3.11+ is the only runtime requirement. From the repository root run:

```sh
python3 mobile-observatory/run.py
```

Windows: `py -3 mobile-observatory/run.py`.
Open <http://127.0.0.1:8124/>. Stop with Ctrl+C. No package installation is needed.
The older `start.sh` remains the explicitly synthetic demonstration launcher;
use `run.py` for the included real dataset.

## Included data

`mobile-observatory/portable/mobile-observatory-2026-09-17.zip` contains:

- Current `corpus.sqlite` and `local.sqlite` from the validated reviewed snapshot.
  Local settings, acknowledgements, review decisions and collection queue are included.
- Previous reviewed `corpus.sqlite` and `local.sqlite` under `history/review-20260916/`.
- The legacy crawler's `devices.db` under `legacy/`, retained for reference.
- The original captured ingestion ledger, raw evidence, immutable specification
  captures, agent review bundle, validation results and original evidence paths.
- An internal manifest with every packaged file's SHA-256 and byte size, all corpus
  table counts, source observation cutoff and package timestamp. The adjacent
  `.json` file records the whole ZIP checksum and database inventory.

All five database files are consistent SQLite backups, including committed WAL
state. Temporary WAL/SHM sidecars are unnecessary. The app source, migrations,
fixtures, product/architecture/identity documents, tests and captured replay
inputs under `crawler/relay/results` are also in the repository.

The corpus contains 83 reviewed hardware models, 21,186 canonical firmware
releases, 4,886 product ROM records, 862 product security publications, 616 source
products and 33 product silicon observations. Four exact hardware-silicon
mappings remain separate. Source observations run through 2026-09-16T06:00:00Z;
the packaging date is not a live collection date. Specifications have their own
captured observation timestamps.

## First launch and moving computers

The launcher verifies the ZIP, checks all extracted files and databases, then
atomically creates `mobile-observatory/.observatory-data`. It rebases active
artifact storage paths to that directory; original paths remain in
`provenance-paths.json`. It never overwrites an existing data directory. That
runtime directory is ignored by Git, so local changes do not dirty the repository.
The distributed archive remains immutable; running databases are writable.

Options:

```sh
python3 mobile-observatory/run.py --port 8125
python3 mobile-observatory/run.py --data-dir /path/to/my-observatory
python3 mobile-observatory/run.py --data-dir /path/to/my-observatory --restore-only
```

To move changes made after this packaged snapshot, stop the app and copy the whole
runtime data directory to the other computer, then pass it with `--data-dir`.
Do not copy a running SQLite database without using SQLite's backup API.
Use a new directory to inspect the packaged baseline again; deleting an existing
runtime directory would discard its later changes.

The preserved historical and legacy databases are audit/reference copies; the
launcher uses the top-level current databases. Manual collection remains captured
replay, using repository-relative source inputs. Missing hardware identities,
Android versions, security applicability or download links remain unknown.

## Verification

```sh
cd mobile-observatory
python3 -m unittest discover -s tests -v
```

`pytest` and Node are optional development tools, not runtime dependencies:
`python3 -m pytest -q` and `node --test apps/web/api.test.mjs`.
