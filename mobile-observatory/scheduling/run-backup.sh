#!/bin/sh
# Back up the part of the corpus that cannot be rebuilt.
#
# NOT corpus.sqlite. That is 243 MB and almost all derived: the captured inputs
# are in git and a batch rebuilds it. What this saves is what a rebuild cannot
# reproduce -- every file an `artifacts` row cites inside the data directory,
# plus local.sqlite's human decisions. Measured on the live corpus: 20 files,
# 20.6 MB raw, 2.0 MB compressed, against 243 MB for the database.
#
# Read ../docs/BACKUP.md before relying on this, in particular the section on
# why a rebuild is NOT a restore.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DATA_DIR="${MOBILE_OBSERVATORY_DATA_DIR:-$APP_ROOT/.observatory-data}"
BACKUP_DIR="${MOBILE_OBSERVATORY_BACKUP_DIR:-$APP_ROOT/.observatory-backups}"
KEEP="${MOBILE_OBSERVATORY_BACKUP_KEEP:-14}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$BACKUP_DIR/evidence-$STAMP.tar.gz"

cd "$APP_ROOT"
# Exit 2 means the archive was written and verifies but does not cover
# everything -- e.g. local.sqlite was absent. That is worth a human's
# attention, so it is NOT swallowed here; the caller's failure notification
# should fire on it exactly as it does for a real error.
python3 -u tools/backup_evidence.py --data-dir "$DATA_DIR" --output "$ARCHIVE"
status=$?

# Verify what was just written. An unverified backup is a belief, and the
# cheapest moment to discover it is unreadable is now rather than during a
# restore.
python3 -u tools/backup_evidence.py --verify "$ARCHIVE" >/dev/null

# Prune by count, oldest first. Deliberately AFTER a successful verify, so a
# run that produced a bad archive never deletes a good one.
ls -1t "$BACKUP_DIR"/evidence-*.tar.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
done

echo "backup: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1)), keeping $KEEP"
exit $status
