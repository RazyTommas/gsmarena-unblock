#!/usr/bin/env bash
# refresh.sh — update the firmware corpus in place. Safe to run on a schedule.
# The Atlas/Watch apps read the DB live, so the lists update with no restart.
cd "$(dirname "$0")"
PY=""; for c in python3 python; do command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }; done
[ -z "$PY" ] && { echo "Python 3 not found"; exit 1; }
ts(){ date '+%Y-%m-%d %H:%M:%S'; }

echo "[$(ts)] refresh start"
# 1) Apple iPhones — clean public API, always refreshable; then (re)attach specs + links
"$PY" ios.py            && echo "[$(ts)] iOS refreshed"      || echo "[$(ts)] iOS refresh FAILED"
"$PY" add_iphones.py    && echo "[$(ts)] iPhone specs linked" || echo "[$(ts)] iPhone specs FAILED"
# 2) Samsung 'new versions in line' check (writes nothing to corpus; logs pending)
"$PY" check_updates.py --only-updates 2>/dev/null | head -20
# 3) chipset enrichment continues where it left off (resumable, best-effort)
"$PY" enrich_specs.py --vendors samsung tecno xiaomi --timeout 12 >/dev/null 2>&1 &
echo "[$(ts)] refresh done (chipset enrichment continues in background)"
