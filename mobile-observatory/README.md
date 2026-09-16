# Mobile Observatory Core

## Download and run the complete reviewed snapshot

This repository includes the real **2026-09-17 portable snapshot**, databases and
captured evidence. It requires **Python 3.11 or newer**; no pip install, Node,
API key, external database, or network access is needed to run Mobile Observatory.

From the downloaded or cloned `mobile-observatory` directory:

```sh
python3 run.py
```

On Windows:

```powershell
py -3 run.py
```

Open <http://127.0.0.1:8124/>. First launch verifies and extracts the packaged data;
subsequent launches preserve your existing local database. See
[portable snapshot contents and migration instructions](docs/PORTABLE_RUN.md).


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
python -m unittest discover -s tests -v
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
