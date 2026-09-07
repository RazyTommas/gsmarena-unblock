#!/usr/bin/env bash
# Firmware Watch — preflight + launch. Double-click or run: bash START.sh
cd "$(dirname "$0")"
echo "== Firmware Watch preflight =="
PY=""
for c in python3 python; do command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }; done
[ -z "$PY" ] && { echo "FAIL: Python 3 not found. Install it from https://python.org (or your package manager)."; exit 1; }
V=$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
echo "OK  Python $V ($PY)"
[ -f baseline_latest.csv ] || { echo "FAIL: baseline_latest.csv missing next to this script."; exit 1; }
echo "OK  baseline_latest.csv present ($(wc -l < baseline_latest.csv) lines)"
echo -n "..  testing internet to Samsung manifest... "
"$PY" - <<'PYEOF'
import urllib.request,sys
try:
    urllib.request.urlopen(urllib.request.Request(
      "https://fota-cloud-dn.ospserver.net/firmware/ILO/SM-S948B/version.xml",
      headers={"User-Agent":"Mozilla/5.0"}),timeout=15).read(200)
    print("OK")
except Exception as e:
    print("WARN: could not reach the manifest ("+type(e).__name__+"). Check network/proxy; the dashboard will still start and retry.")
PYEOF
echo "== launching dashboard on http://localhost:8900 (Ctrl+C to stop) =="
exec "$PY" fw_dashboard.py "$@"
