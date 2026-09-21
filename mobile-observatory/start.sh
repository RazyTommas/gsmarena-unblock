#!/bin/sh
# Serve Mobile Observatory.
#
# This script used to hardcode --demo, so running it always served synthetic
# fixtures -- including when the real corpus was sitting right there. A demo
# flag you cannot see is indistinguishable from real data at a glance, which is
# exactly the failure this project keeps guarding against. Demo is now opt-in.
#
#   ./start.sh              real corpus
#   ./start.sh --demo       synthetic fixtures
#   ./start.sh --port 8937  any server flag passes through
set -eu
cd "$(dirname "$0")"
exec env PYTHONPATH=src python3 -m mobile_observatory.server "$@"
