# Firmware Atlas — device specs × firmware, all vendors

A local, self-contained system that gathers device firmware (Android + iPhone) into
one SQLite corpus and serves two zero-dependency web apps to explore and monitor it.
No login, no lab infra — just Python 3 stdlib (+ `selectolax` for HTML parsing).

## One app, one port

`python3 app.py` → **http://localhost:8765** (or `bash START-ALL.sh`). A single
zero-dependency service with four tabs, all on the shared server-side API:

- **Devices** — spec'd devices (Android + iPhone); click one for the drawer (firmware, specs, live "check for newer").
- **ROMs** — every firmware build, **server-side** filtered/sorted/paginated (200/page). Filters: vendor · source · region · Android · FW-type · device name · chipset · **date range** · Latest-only · Dated-only.
- **Watch** — cross-vendor dashboard: newest release, latest build per vendor, recent releases.
- **Insights** — aggregate charts + pivot.

Architecture note: filtering/sorting/paging run as SQL on the server (indexed SQLite), so the
browser only ever holds one page — it stays fast whether the corpus is 50k or millions of rows.
(`fw_dashboard.py` is the retired standalone zone-watch; the Watch tab replaces it.)

## Data sources (all no-login)

| Source | Vendor | Via | Dates? | Chipset? |
|--------|--------|-----|:------:|:--------:|
| **ipsw.me** API | iPhone (iOS) | `ios.py` | ✅ | curated (`add_iphones.py`) |
| **mifirm.net** | Xiaomi/Redmi/Poco | `mifirm.py` | ✅ | — |
| **samfw.com** | Samsung | browser-ingest | ✅ | via gsmarena/Wayback |
| **androidmtk / naijarom** | Tecno | `androidmtk` index | approx | — |
| **archive.org** | mixed | `scan_archive.py` | metadata | — |
| firmwarefile / romprovider / needrom | mixed | parsers | — | — |
| **gsmarena.com** | specs (all) | Wayback bypass (`vendor/wayback_fallback.py`) | — | ✅ |

`check_updates.py` compares our newest Samsung build per CSC against Samsung's public
`fota-cloud` manifest to flag "new versions in line".

## The corpus (`data/devices.db`, gitignored — ships in the bundle)

- **`devices`** — one row per spec'd device (gsmarena Android + curated iPhones), ~163 rows,
  columns `Section — Field` (Platform — Chipset/CPU/GPU, Network — Modem, Body, Display…).
- **`roms`** — every firmware build (~51k): source, device, model, region, type, version,
  android, size, updated_at, download_url, matched_devices.
- **`device_specs`** — chipset cache (device → chipset), joined onto the ROMs view.

## Scripts (all cwd-independent; import shared helpers from `common.py`)

| Script | Purpose |
|--------|---------|
| `common.py` | shared: `DB_PATH`, `http_get()` (UA + retry), `decode_pda()` / `pda_month()` |
| `ios.py` | ingest iPhone firmware from ipsw.me (all versions) |
| `add_iphones.py` | add iPhones to `devices` with curated chipset/CPU/GPU/modem/display/battery/date; link firmware |
| `enrich_specs.py` | fill Android chipset from archived gsmarena (Wayback), into `device_specs` |
| `check_updates.py` | Samsung "new versions in line" vs fota-cloud (exit code = count) |
| `crawler.py`, `mifirm.py`, `samfw.py`, `firmwarefile.py`, `romprovider.py`, `scan_archive.py` | source parsers |
| `export.py` | build/refresh the SQLite corpus from crawled JSON |
| `find_fw.py` | on-demand deep search for a specific firmware/version |

## Keep it updated (every few hours)

`bash refresh.sh` re-pulls iOS → relinks iPhone specs → checks Samsung freshness →
continues Android chipset fill. The apps read the DB live, so lists update with no restart.
Cron example:

```
0 */4 * * * cd /path/to/fw_atlas_full && bash refresh.sh >> refresh.log 2>&1
```

## Offline bundle

`fw_atlas_full/` is a copy-to-USB kit (apps + scripts + `data/devices.db` + launchers).
`START-ALL.sh` / `START-ATLAS.sh` / `START-WATCH.sh` (and `.bat` equivalents) preflight
Python + data + connectivity, then launch.
