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

cd "$APP_ROOT"

# -u: fully unbuffered stdio. Belt-and-suspenders alongside batch.py's own
# line-buffered log file (src/mobile_observatory/batch_logging.py) -- see
# docs/SOURCE_SILENCE_DETECTION.md for why both exist. batch.py writes its
# real log to $DATA_DIR/batch.log regardless of how this stdout is handled;
# stdout/stderr here are only the JSON summary and duplicate log lines,
# useful for `journalctl` or a cron MAILTO, not the durable record.
exec env PYTHONPATH="$APP_ROOT/src" python3 -u -m mobile_observatory.batch \
  --data-dir "$DATA_DIR" \
  --legacy-root "$LEGACY_ROOT"
