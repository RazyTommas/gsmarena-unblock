#!/usr/bin/env bash
# Build a sizable real corpus across all working sources. Polite delays; caches HTML.
set -u
cd "$(dirname "$0")"
D=1.0
echo "=== gsmarena Samsung (120) ==="   ; python3 crawler.py samsung --limit 120 --delay $D 2>&1 | tail -1
echo "=== gsmarena Xiaomi (80) ==="     ; python3 crawler.py xiaomi  --limit 80  --delay $D 2>&1 | tail -1
echo "=== mifirm ALL models ==="        ; python3 crawler.py --mifirm --delay $D 2>&1 | tail -1
echo "=== firmwarefile samsung (120) ="; python3 crawler.py --ff samsung --limit 120 --delay $D 2>&1 | tail -1
echo "=== firmwarefile infinix (60) ==="; python3 crawler.py --ff infinix --limit 60 --delay $D 2>&1 | tail -1
echo "=== firmwarefile itel (50) ==="   ; python3 crawler.py --ff itel    --limit 50 --delay $D 2>&1 | tail -1
echo "=== firmwarefile realme (50) ===" ; python3 crawler.py --ff realme  --limit 50 --delay $D 2>&1 | tail -1
echo "=== firmwarefile oppo (50) ==="   ; python3 crawler.py --ff oppo    --limit 50 --delay $D 2>&1 | tail -1
echo "=== firmwarefile vivo (50) ==="   ; python3 crawler.py --ff vivo    --limit 50 --delay $D 2>&1 | tail -1
echo "=== ALL DONE ==="
