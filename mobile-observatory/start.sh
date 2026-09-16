#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec env PYTHONPATH=src python3 -m mobile_observatory.server --demo "$@"
