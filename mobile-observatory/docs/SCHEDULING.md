# Scheduled batch

Every ingest this system has ever done was a person running
`python3 -m mobile_observatory.batch` by hand. This document is a ready-to-use
way to run it on a schedule instead. **Nothing here is installed or enabled by
this repository.** A human decides when to turn it on.

## What runs

`scheduling/run-batch.sh` is the single entry point both options below call.
It resolves paths relative to the repository, then execs:

```sh
PYTHONPATH=<repo>/mobile-observatory/src python3 -u -m mobile_observatory.batch \
  --data-dir <repo>/mobile-observatory/.observatory-data \
  --legacy-root <repo>/crawler/relay/results
```

Override `MOBILE_OBSERVATORY_DATA_DIR` / `MOBILE_OBSERVATORY_LEGACY_ROOT` if
your installation's paths differ from this repository's own layout, and the
`MOBILE_OBSERVATORY_RETENTION_*` / `MOBILE_OBSERVATORY_LOG_*` variables in the
table further down to change what the batch prunes and rotates.

`batch.py` writes a durable, line-buffered log to `<data-dir>/batch.log`
regardless of how stdout/stderr are handled by systemd or cron. It is rotated at
a byte cap by the batch itself -- see "Log growth and ledger growth" below, which
also covers the two ledger trees that used to grow without bound. On a real
copy of the captured legacy corpus this repository ships with, one run takes
several minutes (dominated by SQLite dedupe/enrichment over tens of
thousands of rows) -- size your timer/cron interval and any
`TimeoutStartSec` accordingly.

## Exit codes

- **0** -- the batch ran and no source is overdue.
- **2** -- the batch ran successfully, but the silence detector found at
  least one source overdue for its next run. This is an **advisory** signal:
  the run itself did not fail, and no data was rejected or blocked. See
  `docs/SOURCE_SILENCE_DETECTION.md`.
- **any other nonzero** -- the batch itself raised and did not complete.

A cron/systemd wrapper cannot tell exit code 2 apart from a real crash by
exit code alone unless you add a small check for `$?  -eq 2` in your own
alerting; both count as a failed run to systemd's `ActiveState` and to
cron's own nonzero-exit mail trigger, which is enough for most setups.

## Option A: systemd user timer (recommended)

1. Copy `scheduling/mobile-observatory-batch.service` and
   `scheduling/mobile-observatory-batch.timer` to
   `~/.config/systemd/user/`.
2. Edit the two `/CHANGE/ME/...` paths in the `.service` file to your actual
   checkout location.
3. Adjust `OnCalendar=` in the `.timer` file if daily-at-03:00 doesn't match
   how often your captured evidence actually changes.
4. Enable it:

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now mobile-observatory-batch.timer
   ```

5. Check it:

   ```sh
   systemctl --user list-timers mobile-observatory-batch.timer
   journalctl --user -u mobile-observatory-batch.service
   ```

If your system does not keep user services running after logout, also run
`loginctl enable-linger "$USER"` (as root or via sudo), or install the unit
as a system service under `/etc/systemd/system/` instead (drop `--user`
everywhere and run as root).

## Option B: cron

1. Copy the line from `scheduling/crontab.example` into `crontab -e`.
2. Edit the `/CHANGE/ME/...` path and the `MAILTO` address.
3. `MAILTO` only works if the machine has a working local MTA. If it
   doesn't, cron will silently drop that mail -- the log file and the admin
   health API (below) are the fallback.

## What "the alarm reaching someone" actually means here

Turning either of the above on gets you a batch that runs unattended and
that *can* fail loudly (log line + nonzero exit) when a source goes silent.
It does not, by itself, page anyone. Two things already consume the
detector's output without any further setup:

- **`<data-dir>/batch.log`** gets an `ALARM source silent (advisory): ...`
  line every run a source is overdue. Tail it, or point your existing log
  shipper at it.
- **The already-running web app's admin Operations page and its
  `sourceWarnings` count** reflect the same detector on every page load
  (`GET /api/v1/admin/health`, `GET /api/v1/radar/overview`) -- no batch run
  required to see it, since it is computed live from `ingestion_runs`.

Turning the nonzero exit code into a page/email/Slack message is on you: it
requires wiring `OnFailure=`/cron `MAILTO`/a monitoring check to something
that actually notifies a person. See `docs/SOURCE_SILENCE_DETECTION.md` for
why this is deliberately left advisory rather than silently escalated.

## Log growth and ledger growth

Both are bounded by the batch itself now, in
`src/mobile_observatory/retention.py`. It runs **inside the batch lock and
before the ingest**, prints one `retention ...` line to `batch.log`, and puts
the whole account in the JSON summary under `"retention"`.

### What it manages, and what it refuses to touch

| tree | measured | policy |
|---|---|---|
| `ledger/staging` | 94,272,853 B (89.9 MiB) | age + byte budget |
| `ledger/raw` | 19,202,294 B (18.3 MiB) | age + byte budget, **behind a veto** |
| `ledger/quarantine` | 0 B (15 empty files) | age + byte budget |
| `batch.log` | 30,133 B / 130 lines | rotated at a byte cap, N generations |

Everything else in the data directory is deliberately out of scope:

- **`ledger/runs/`** -- 9,518 B, and `latest.json` is live state. A run's own
  record is the only evidence it happened; the bytes are not here.
- **`changesets/`** -- 7.1 MB, growing ~285 KB per batch (~104 MB/year). Adding
  it is one `TreeRule` in `retention.default_policy()`. It is left out because a
  changeset is the only way to revert a run (`docs/CHANGESETS.md`) and how far
  back a revert must reach is your call, not a default.
- **`history/review-20260916/`** -- 69 MB of a frozen human snapshot. Nothing in
  `src/` writes it, so nothing in `src/` deletes it.
- **`cron.log` and `server.log`** -- shell redirects from the wrapper and the
  server, not files this process owns. A `>>` redirect holds the inode open, so
  renaming one leaves the writer appending to the renamed file, silently. Use
  `logrotate` with **`copytruncate`** for those two if they ever matter; at
  109 KB and 8 KB they do not yet.

### The knobs

Every default is measured. `--help` carries the short version.

| flag | `MOBILE_OBSERVATORY_*` | default | why that number |
|---|---|---|---|
| `--retention-days` | `RETENTION_DAYS` | `14` | fourteen consecutive nightly runs in which a run id was not re-emitted. What is lost is a derived parse; its basis (the raw artifact) is vetoed and stays. |
| `--retention-max-mb` | `RETENTION_MAX_MB` | `256` | 2.4x the measured 108.2 MiB of managed ledger. Does not fire today, so no churn; fires when the ledger more than doubles. |
| `--retention-min-age-hours` | `RETENTION_MIN_AGE_HOURS` | `48` | two nightly cadences. This is what makes it impossible for the byte budget to take a file the current or previous run wrote -- 91,902,560 B, 97.5% of `ledger/staging`. |
| `--log-max-mb` | `LOG_MAX_MB` | `4` | at the measured 232 B per run this will not fire for decades. It is a bound against a verbosity regression, not against today's growth. |
| `--log-keep` | `LOG_KEEP` | `4` | worst case 20 MiB of log, and `keep` alone bounds nothing -- the byte cap is what makes the count mean something. |
| `--retention-dry-run` | `RETENTION_DRY_RUN` | off | report and change nothing. |
| `--retention-off` | `RETENTION_OFF` | off | skip it; the log says so rather than reporting a run that pruned nothing. |

`scheduling/run-batch.sh` passes each flag **only when its variable is set**, so
an unset variable means "whatever `batch.py --help` says". The wrapper
deliberately keeps no copy of any default.

### Why it will not delete your evidence

A **veto**, built from `artifacts.storage_uri` *and* `artifacts.sha256`, and
reported file by file with the rule that saved each one. Three things make it
more than a path check:

- `ledger/raw` is content-addressed and write-once, so **mtime is not a liveness
  signal** -- every raw file on the live corpus has a September mtime while
  today's run cites all 13 of its digests. Age alone would delete the tree.
- The digest half is what survives **relocation**, which is what a restore is.
  Measured: 5 of 14 raw `.bin` files are cited only at an `evidence/artifacts/`
  path holding the same bytes, so a path-only veto would have offered them
  (plus their 5 sidecars) to the byte cap.
- A raw `.bin` and its `.json` sidecar are **indivisible**:
  `collectors/importer.import_run` opens the sidecar first and the body second,
  so losing either half makes a replay raise `FileNotFoundError`.

A staging file still referencing a digest also vetoes it, which is why staging is
planned before raw.

When the byte budget **cannot** be met from eligible files it says so --
`residual_bytes`, `residual_reason`, and an `ALARM` line -- instead of deleting a
cited or current-run file to reach the number. The next run would write it again
and the budget would still be missed, so that would be churn with data loss
attached. Raise the cap deliberately, or lower `--retention-days`.

With no corpus to ask (a first build), retention **refuses the whole prune** and
logs a plain line, not an `ALARM`: with no citation basis every raw file looks
uncited, which is the one mistake this exists to prevent.

### Measured effect, today

On a copy of the live corpus with the defaults: **8 files, 2,370,293 B** out of
`ledger/staging` and `ledger/quarantine` -- the four retired run ids and the
manual collection worker's one-off, and nothing else. `ledger/raw` yields
**zero bytes**: all 13 of its digests are cited *and* staged, so 28 files /
19.2 MB are 100% vetoed. The open item in `HANDOFF.md` that named `ledger/raw`
as the tree growing without bound named the only one of the two this policy can
never free a byte from.

### If you would rather do it yourself

`--retention-off` leaves every tree alone and says so in the log. A `logrotate`
rule for `batch.log` then needs `copytruncate`, for the same reason `cron.log`
does: `batch_logging.configure_batch_logging` holds the file open for the whole
run.
