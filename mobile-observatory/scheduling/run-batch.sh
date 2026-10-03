#!/bin/sh
# Wrapper the systemd unit and the cron example in this directory both call.
# Nothing in this repository installs or enables either of them -- see
# ../docs/SCHEDULING.md to turn this on.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Override with real values for your installation, e.g. via the systemd
# unit's EnvironmentFile= or by exporting them before calling this script
# from cron. Defaults match this repository's own layout.
DATA_DIR="${MOBILE_OBSERVATORY_DATA_DIR:-$APP_ROOT/.observatory-data}"
LEGACY_ROOT="${MOBILE_OBSERVATORY_LEGACY_ROOT:-$APP_ROOT/../crawler/relay/results}"

# Retention knobs. Deliberately NOT given a default here: batch.py owns every
# default (see src/mobile_observatory/retention.py for the measurement behind
# each one), and a second copy of a number in a wrapper script is how the two
# drift apart until a change to one of them silently does nothing. Each flag is
# appended only when its variable is set, so an unset variable means "whatever
# batch.py's --help says", not "whatever this file last said".
RETENTION_FLAGS=""
[ -n "${MOBILE_OBSERVATORY_RETENTION_DAYS:-}" ] && \
  RETENTION_FLAGS="$RETENTION_FLAGS --retention-days $MOBILE_OBSERVATORY_RETENTION_DAYS"
[ -n "${MOBILE_OBSERVATORY_RETENTION_MAX_MB:-}" ] && \
  RETENTION_FLAGS="$RETENTION_FLAGS --retention-max-mb $MOBILE_OBSERVATORY_RETENTION_MAX_MB"
[ -n "${MOBILE_OBSERVATORY_RETENTION_MIN_AGE_HOURS:-}" ] && \
  RETENTION_FLAGS="$RETENTION_FLAGS --retention-min-age-hours $MOBILE_OBSERVATORY_RETENTION_MIN_AGE_HOURS"
[ -n "${MOBILE_OBSERVATORY_LOG_MAX_MB:-}" ] && \
  RETENTION_FLAGS="$RETENTION_FLAGS --log-max-mb $MOBILE_OBSERVATORY_LOG_MAX_MB"
[ -n "${MOBILE_OBSERVATORY_LOG_KEEP:-}" ] && \
  RETENTION_FLAGS="$RETENTION_FLAGS --log-keep $MOBILE_OBSERVATORY_LOG_KEEP"
[ -n "${MOBILE_OBSERVATORY_RETENTION_DRY_RUN:-}" ] && \
  RETENTION_FLAGS="$RETENTION_FLAGS --retention-dry-run"
[ -n "${MOBILE_OBSERVATORY_RETENTION_OFF:-}" ] && \
  RETENTION_FLAGS="$RETENTION_FLAGS --retention-off"

cd "$APP_ROOT"

# -u: fully unbuffered stdio. Belt-and-suspenders alongside batch.py's own
# line-buffered log file (src/mobile_observatory/batch_logging.py) -- see
# docs/SOURCE_SILENCE_DETECTION.md for why both exist. batch.py writes its
# real log to $DATA_DIR/batch.log regardless of how this stdout is handled;
# stdout/stderr here are only the JSON summary and duplicate log lines,
# useful for `journalctl` or a cron MAILTO, not the durable record.
# $RETENTION_FLAGS is deliberately UNQUOTED: it has to split into separate
# argv entries. Every value in it came from a numeric env var above, and an
# empty string splits to nothing, which is the "use batch.py's default" case.
# shellcheck disable=SC2086
exec env PYTHONPATH="$APP_ROOT/src" python3 -u -m mobile_observatory.batch \
  --data-dir "$DATA_DIR" \
  --legacy-root "$LEGACY_ROOT" \
  $RETENTION_FLAGS
