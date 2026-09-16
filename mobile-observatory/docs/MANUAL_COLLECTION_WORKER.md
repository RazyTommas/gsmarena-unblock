# Manual collection worker

Admin collection requests are durable jobs with the states `queued`, `running`,
`succeeded`, `partial`, and `failed`. Each job stores timestamps, a run ID,
structured logs, and a result summary.

The current worker is intentionally an **offline captured-artifact replay
worker**, not a live network collector. API responses expose
`execution_mode: captured_replay` and `live_network: false`. Supported dispatches:

- Xiaomi firmware/latest/profile requests replay the captured tracker CSV.
- Tecno security requests replay the captured vendor security CSV.
- Samsung firmware history requests replay the captured FOTA history CSV.
- Samsung latest firmware is available for the captured SM-S938B/ILO manifest.

Unsupported source/scope combinations become failed jobs with an explanatory
log; they never silently report a refresh.

Run the oldest queued request:

```sh
PYTHONPATH=src python -m mobile_observatory.collection_worker \
  --data-dir .observatory-data \
  --legacy-root ../crawler/relay/results
```

Or add `--request-id 42`. The API equivalents are:

- `POST /api/v1/admin/collection-requests/{id}/process`
- `POST /api/v1/admin/collection-requests/process-next`

Live collection is a separate adapter capability to add later. It should use
the same queue contract, preserve raw evidence, identify its execution mode as
live, apply timeouts/rate limits, and never fall back to replay without saying
so in the result.
