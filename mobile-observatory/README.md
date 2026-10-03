# Mobile Observatory Core

## Build and run the corpus

Requires **Python 3.11 or newer**. No pip install, Node, API key, external
database, or network access at runtime. All 16 captured inputs the batch needs
are in git (24 MB), so it takes no flags.

From the cloned `mobile-observatory` directory:

```sh
PYTHONPATH=src python3 -m mobile_observatory.batch
PYTHONPATH=src python3 -m mobile_observatory.server --data-dir .observatory-data
```

Open <http://127.0.0.1:8000/>. The batch runs for about ten minutes and logs per
source as it goes; it prints a JSON summary when it finishes. Measured on a real
clone, 2026-09-29: **854 devices / 94,969 observations, 9m50s**.

On Windows, substitute `py -3` for `python3` and `set PYTHONPATH=src`.

### Do not start from `python3 run.py`

This README used to lead with it, and it is why this section exists. `run.py`
extracts a **packaged historical snapshot**, which is a tenth of the above:
measured **83 devices / 26,961 observations** from the 2026-09-17 bundle. Nothing
downstream reported anything wrong — every page was internally consistent and
answered from a corpus missing 771 phones.

`python3 run.py` now refuses and names the two commands above. The snapshot is
still reachable with `python3 run.py --bundle` for a box that genuinely has no
captured inputs, and `python3 run.py --list-bundles` reports what each packaged
snapshot states it holds. See
[portable snapshot contents and migration instructions](docs/PORTABLE_RUN.md).

**A rebuild is not a restore.** Rebuilding from the same inputs does not
reproduce an existing corpus — measured, 201 of 865 devices resolve differently.
If you need a specific corpus, copy its `.observatory-data/`; see
[docs/BACKUP.md](docs/BACKUP.md). `PYTHONPATH=src python3 -m mobile_observatory.integrity`
reports whether a corpus still matches the identity conclusions it was built
with.


Greenfield domain core for a local-first mobile device, firmware, silicon, and
security intelligence system. This directory is deliberately independent of
the legacy crawler and database. Legacy data is an input to reviewed import
work, never an API or schema constraint.

## Guarantees

- Raw source observations and canonical facts are different records.
- Commercial devices, sellable variants, hardware models, and firmware targets
  are different identities.
- Silicon families, parts, and revisions are different identities.
- Unknown, not applicable, and not adjudicable are different security states.
- All canonical relationships can carry provenance.
- Change events are append-only and idempotent.
- Agent output can only enter the proposal queue.

## Run the core tests

```sh
cd mobile-observatory
python3 -m unittest discover -s tests -v
```

## Run the demonstration end to end

The bundled catalog is synthetic and the server labels every response and the
dashboard snapshot as `DEMONSTRATION`:

```sh
cd mobile-observatory
PYTHONPATH=src python3 -m mobile_observatory.server --demo
```

Open <http://127.0.0.1:8000>. Data is stored under `.observatory-data/` as a
replaceable `corpus.sqlite` and separate `local.sqlite`. The server refuses to
start an empty corpus without the explicit `--demo` flag.

The interface includes system/light/dark/high-contrast themes, canonical global
search, hierarchical silicon filters (vendor → family → exact part), full
observed ROM history, CSV export, local operator preferences, configuration
import/export, and a model-resolution workflow. Admin also contains a dated
real-source evaluation sample across Samsung, Xiaomi, and Tecno tiers; unresolved
source identities remain explicitly unpromoted.

Entity names and chip parts are clickable. Device details combine current state,
last observation time, full firmware history by region, modem/baseband, patch
levels, silicon, and linked security findings. Chip details reverse the relation
to devices and advisories. Identity decisions are durable local state documented
in [docs/IDENTITY_RESOLUTION.md](docs/IDENTITY_RESOLUTION.md).

Do not attach the legacy database directly. Production corpus integration must
go through reviewed import plus server-side pagination/materialized read models;
the demonstration API is intentionally small and is not a scale benchmark.

Build and verify a portable offline snapshot after starting the demo once:

```sh
PYTHONPATH=src python3 -m mobile_observatory.snapshots build \
  --corpus .observatory-data/corpus.sqlite --web apps/web --output observatory-offline
PYTHONPATH=src python3 -m mobile_observatory.snapshots verify observatory-offline
```

The runtime uses only the Python standard library. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for component boundaries and [docs/PRODUCT.md](docs/PRODUCT.md) for the approved
product behavior.

## Batch ingest and scheduling

`PYTHONPATH=src python3 -m mobile_observatory.batch` replays every captured source into the
corpus; until now it has only ever been run by hand. `scheduling/` has a
ready-to-enable systemd timer and cron alternative (not installed or enabled
by this repository) — see [docs/SCHEDULING.md](docs/SCHEDULING.md). Every
batch run, and every load of the admin health API, also checks each source
for having gone quiet relative to its own history; see
[docs/SOURCE_SILENCE_DETECTION.md](docs/SOURCE_SILENCE_DETECTION.md).
