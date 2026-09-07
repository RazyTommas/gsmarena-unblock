FIRMWARE WATCH — standalone dashboard
=====================================
What it is: a self-contained dashboard that checks Samsung's PUBLIC firmware
version manifest on a routine timer and shows, for the Israel (ILO) and
Iraq/Lebanon (MID) zones: the latest release, what is "new in line" (a newer
build than ours), and the current build per device. It pops the newest release
(banner + browser toast + best-effort desktop notification).

Requirements on the target machine:
  * Python 3.7 or newer  (nothing else — no pip installs, no internet libs)
  * Outbound internet access (to fota-cloud-dn.ospserver.net)

Run it:
  Linux / macOS : bash START.sh
  Windows       : double-click START.bat
Then open:  http://localhost:8900

Options (append after the command, e.g. bash START.sh --interval 120):
  --interval N   check every N minutes (default 360 = every 6 hours)
  --host 0.0.0.0 also serve to other devices on the LAN (default localhost only)
  --port P       change the port (default 8900)
  --once         run a single check, print the result, and exit (no server)

Files:
  fw_dashboard.py     the dashboard (single file)
  baseline_latest.csv the 49 devices + our last-known builds (edit to add devices)
  fw_state.json       created on first run; remembers what was seen (for "new" pops)

It is a separate system: it needs nothing from the crawler database or any lab
infrastructure. To update the "ours" baseline later, replace baseline_latest.csv.
