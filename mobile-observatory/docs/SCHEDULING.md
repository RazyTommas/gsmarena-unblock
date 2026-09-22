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
your installation's paths differ from this repository's own layout.

`batch.py` writes a durable, line-buffered log to `<data-dir>/batch.log`
(append-only; nothing here rotates or truncates it -- see "Log growth" below)
regardless of how stdout/stderr are handled by systemd or cron. On a real
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

## Log growth

`batch.log` is opened in append mode and is never rotated by anything in
this repository. If you enable scheduling, also add a `logrotate` rule (or
periodically truncate it yourself) once you have a real cadence in mind --
that was out of scope here since it depends on how often you actually run
this and how much history you want to keep.
