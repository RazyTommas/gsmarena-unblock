# Source silence detection

A collector that silently stopped looks identical to one with nothing to
report. `src/mobile_observatory/silence.py` is the one place that tells
those apart, by comparing each source's own history against itself.

## Algorithm

For every row in `sources`, look at its `ingestion_runs`:

- **0 or 1 finished runs** -> `insufficient_data`. There is no cadence to
  violate yet, so this is never reported as silence. A brand-new source is
  not a broken one.
- **>= 2 finished runs** -> the median gap between consecutive runs' start
  times is the source's own expected interval. The threshold for "overdue"
  is `max(expected_interval * 3, 1 hour)`. If the time since the source's
  last activity (finished run, or the start of a still-`running` one)
  exceeds that threshold, the source is `silent`; otherwise `healthy`.

The baseline is learned per source, not hardcoded, because sources here have
genuinely different natural cadences (a manually re-run captured-replay
adapter vs. a source meant to run hourly). `multiplier` (default 3.0) and
`min_grace_hours` (default 1.0) are keyword arguments on `detect_silence()`
if a future caller needs to tune sensitivity per source; nothing currently
overrides the defaults.

## This is advisory

`detect_silence()` only reads `sources`/`ingestion_runs` and returns
findings. It never writes to the corpus, never blocks a batch run, never
retries or disables a source, and never changes what data is accepted.
Every finding returned by the API carries `silenceAdvisory: true` for the
same reason: nothing downstream may mistake a label for a gate.

## Where it's wired in (the "detector must reach an actor" requirement)

1. **`GET /api/v1/admin/health`** -- each row gains `silenceStatus`,
   `silent`, `expectedIntervalHours`, `overdueHours`, `silenceAdvisory`.
   This is the same endpoint the web app's admin Operations table already
   renders (`apps/web/app.js`), so a human looking at that page during a
   normal visit sees a silent source without anything else running.
2. **`GET /api/v1/radar/overview` -> `sourceWarnings`** -- counts a source
   once if its last run failed **or** it is silent (an OR, not a sum, so a
   source that is both failed and overdue is not double-counted). This
   number is the metric tile the web app's home page shows on every load.
3. **`python3 -m mobile_observatory.batch`** -- every batch run computes
   `results["silence"]`, logs an `ALARM source silent (advisory): ...` line
   per silent source to `<data-dir>/batch.log`, and exits with status `2` if
   any source is silent (see `docs/SCHEDULING.md`). `run_batch()` itself
   (the importable function used by tests and any other caller) has no side
   effects from this beyond the returned `results["silence"]` list --
   logging and the nonzero exit only happen in `main()`, the CLI entry
   point.

Path 1 and 2 reach a human passively, the moment they load a page that
already exists and is already used. Path 3 reaches a human only if the
scheduled job's exit code is wired to real alerting (systemd `OnFailure=`,
cron `MAILTO`, or a monitoring check on the unit/exit code) -- which is a
human decision documented in `docs/SCHEDULING.md`, not something this
change performs automatically. Be honest about that distinction: 1 and 2
are a guard against the specific failure mode of "nobody happens to look";
3 is advisory logging that only becomes a real alarm once someone connects
it to a notification path.

## Testing it

`tests/test_silence.py` plants both a source that must fire (silent) and
one that must not (healthy) from the same synthetic corpus, at the
`detect_silence()` level and again through `ObservatoryService.health()`/
`overview()`, so the wiring into the API is proven, not just the detector
function in isolation. `tests/test_batch_logging.py` proves the batch's
line-buffered logging actually writes bytes to disk **while the process is
still running** (not after it exits, which proves nothing -- interpreter
shutdown flushes regardless of buffering mode), and proves `batch.main()`'s
exit-code/log-ALARM wiring without needing the multi-minute real pipeline.
