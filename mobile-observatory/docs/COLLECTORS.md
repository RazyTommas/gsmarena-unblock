# Collector boundary

Collectors are untrusted source adapters. They cannot write canonical tables or
resolve identities. The ingestion sequence is fixed:

1. fetch and preserve the source response as an immutable, content-addressed artifact;
2. parse it into versioned staging observations;
3. validate required shape and source-local identifiers;
4. quarantine invalid observations with machine-readable reasons;
5. hand valid observations to the separately owned identity/promotion layer.

An observation is evidence that a source made a claim, not proof that the claim is
canonical truth. `identity_hints` are suggestions for the resolver. They must never
contain a silently chosen canonical identity.

## Adapter requirements

- One adapter has one stable `source_id`.
- Fetching performs I/O; parsing must be deterministic and offline.
- Raw artifacts are saved before parsing.
- Every observation links to an artifact SHA-256 and a source-local stable record ID.
- Dates retain their source meaning. Release, publication, retrieval, and observation
  times are distinct fields.
- Empty inventories, sharp count drops, parse failures, and validation errors surface
  as source health states; they are never interpreted as device removal.
- Network adapters require saved fixtures and parser tests before activation.

## Sample seed

The checked-in sample is synthetic and intended for UI/development bootstrapping. It
must not be represented as current firmware truth. Generate the replayable ledger with:

```sh
PYTHONPATH=src python3 -m mobile_observatory.collectors.seed \
  --fixture fixtures/supported_catalog.sample.json --output var/ingestion
```

The output contains `raw/`, `staging/`, `quarantine/`, and `runs/`. Re-running with
the same fixture and run ID produces the same artifact address and staging content.

`IngestionImporter` can then replay that run into the canonical database. It is
deliberately limited to `sources`, `ingestion_runs`, `artifacts`, and `observations`;
canonical promotion remains the resolver's responsibility. UUIDv5 identifiers and
content hashes make repeated imports idempotent.
