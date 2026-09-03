# device-crawler

Gather **device spec sheets** (gsmarena.com) and **firmware/ROMs** (mifirm.net), export
to SQLite + CSV, and browse it all in a tiny local search app.

Pipeline:
```
crawl (crawler.py) ──▶ output/**.json ──▶ export (export.py) ──▶ data/devices.db + CSV ──▶ app (app.py) ──▶ browser
```

* **gsmarena** — one JSON per device, all spec sections, focus = `Network, Launch, Body, Platform, Misc`.
* **mifirm** — one JSON per device (Xiaomi/Redmi/POCO), every firmware build with region,
  type (fastboot/recovery), branch (stable/developer), version, Android, size, date, and a
  **direct download link**.
* **app** — search ANY field, choose which columns to show, click-to-sort, direct links to
  the source page / firmware download. Zero dependencies (Python stdlib only).

## Quick start
```bash
python crawler.py samsung --limit 20        # device specs for a brand
python crawler.py --mifirm "redmi note"     # ROMs for matching models
python export.py                            # build data/devices.db + CSVs
python app.py                               # open http://localhost:8765
```

## Setup
```bash
pip install --user --break-system-packages httpx selectolax
# optional (only needed if gsmarena starts blocking plain HTTP):
pip install --user --break-system-packages playwright && playwright install chromium
```
> Note: plain HTTP currently returns full data (HTTP 200, no Cloudflare challenge),
> so the browser fallback stays dormant. It engages automatically on 403/429/503 or a
> "Just a moment" page. Use `--no-browser` to disable it entirely.

## Usage
```bash
# whole brand, written to output/samsung/
python crawler.py samsung

# cap the run and slow it down (be polite on big families)
python crawler.py xiaomi --limit 50 --delay 3

# choose which sections land in the "focus" block
python crawler.py samsung --sections Network,Launch,Body,Platform,Misc,Battery,Display

# one device page
python crawler.py --url https://www.gsmarena.com/samsung_galaxy_s26_fe_5g-14870.php

# discover brand names/ids/device-counts
python crawler.py --list-brands
```

### mifirm.net ROMs
```bash
python crawler.py --mifirm                 # all models (Xiaomi/Redmi/POCO)
python crawler.py --mifirm "poco" --limit 10
python crawler.py --list-models            # codename / name / regions
python crawler.py --url https://mifirm.net/model/warhol.ttt   # single model
```
Writes `output/mifirm/<codename>_<name>.json`, each with a `roms[]` array and a
`latest_rom` shortcut. `--limit`, `--delay`, `--no-cache`, `--no-browser` all apply.

### firmwarefile.com ROMs (Samsung, Infinix, itel, Realme, Oppo, Vivo, Xiaomi…)
```bash
python crawler.py --ff samsung --limit 40
python crawler.py --ff infinix          # Transsion family (also: itel)
python crawler.py --url https://firmwarefile.com/samsung-sm-s928b   # one device
```
One parser, routed by brand category. Each device page yields the model code and its
download mirrors (Google Drive "Free" + paid). Keyed by **model code**, so it joins to
gsmarena's `Misc — Models` field (see the join below). Served over plain HTTP.

> **samfw.com** is implemented-ready but **Cloudflare-gated** — its managed challenge
> defeats headless Chromium in this environment, so firmwarefile.com is the working
> Samsung source. samfw would need a residential/stealth browser or a solved-challenge
> cookie to crawl. See [FIRMWARE_SOURCES.md](FIRMWARE_SOURCES.md).

## Export to SQLite + CSV
```bash
python export.py            # reads ./output -> data/devices.db, devices.csv, roms.csv
```
* `devices` table — one row per gsmarena device, spec fields flattened to columns
  (`Network — Technology`, `Body — Weight`, …).
* `roms` table — one row per firmware build, with `download_url` + `model_url`.
Both carry a normalized `device_key` so a device and its ROMs can be related.

## Local search app — "Firmware Atlas"
```bash
python app.py               # binds 0.0.0.0:8765 → reachable on the lab LAN
python app.py --host 127.0.0.1   # local-only
python app.py --port 9000        # change port
```
On start it prints both the `localhost` URL and the **lab LAN** URL (e.g.
`http://192.168.5.25:8765`) to share. The `/ingest` endpoint (browser-ingest for
Cloudflare-gated sources) is **localhost-only** even when bound to the LAN.

### Analytics tab (📊)
Opens with **"Explore — build your own view"**: a dynamic pivot — pick *Dataset*
(Devices / Firmware), *Group by* any dimension (vendor, chipset, Android, RAM, region,
launch year, …) and *split by* a second dimension. "Chipset split by vendor",
"region split by source", "Android by vendor" are all just dropdown combos.
**⬇ Export CSV** downloads whatever pivot you built (group × split matrix + totals).

Below it, a colorful dashboard over the whole corpus — 14 charts including a
**model-releases-over-time** timeline (new models per month, stacked by vendor):
firmware releases over time (stacked by source), per-device firmware timeline,
device launch timeline, builds by source / region / Android, devices by vendor,
**top chipsets**, **firmware-type split**, **RAM configurations**, battery-capacity
histogram, Android-version spread. A one-row filter bar (Vendor / Source / Region /
Android / FW type facets + device-name / chipset / min-mAh) cross-filters every chart
**and** the Devices/ROMs tables. Colors are validated colorblind-safe (dataviz skill).
* Switch **Devices** ⇄ **ROMs**; live stat chips (devices / ROM builds / linked / regions).
* **Search any field** — space-separated terms AND-matched across every column
  (`snapdragon 5000`, `EEA recovery poco`). Press `/` to focus, `Esc` to close.
* **Columns** — tick exactly the columns you want (All / Reset / None).
* Click a header to sort.
* **Click a device → detail drawer** = the join: its full spec sheet **and** every
  matched firmware build, grouped by region, with direct `download` buttons and links
  out to gsmarena + mifirm. `Esc` closes it.

### The device ↔ ROM join (multi-source)
`export.py` links a gsmarena device to firmware from **any** source by shared identity keys:
* **Name keys** (mifirm) — manufacturer word stripped, `/`-bundled names split into
  segments, `5G` dropped but `4G` kept (it marks a real variant). e.g. `Xiaomi Redmi
  Note 17 Pro` ↔ mifirm `iolite`.
* **Model-code keys** (firmwarefile) — `SM-A376B`, `X6895`, `RMX…` matched against the
  codes gsmarena lists in its `Misc — Models` field. e.g. `Samsung Galaxy A37` ↔
  `SM-A376B/E/U/U1/W`.

It prints coverage, e.g. `[join] 13/28 devices linked; 102/207 ROM builds linked`.
Devices gain `rom_count`, `codenames` (matched codenames/models), `rom_url`; each ROM
gains `source` + `matched_devices`.

## More firmware sources
See **[FIRMWARE_SOURCES.md](FIRMWARE_SOURCES.md)** — Samsung (samfw.com…), Tecno/Infinix/itel
(Transsion), and universal repos, with the best next parsers to add.

### Options
| flag | meaning |
|------|---------|
| `--limit N` | scrape at most N devices |
| `--delay S` | base seconds between requests (jitter added); raise for large brands |
| `--out DIR` | output root (default `output/`); files go to `output/<brand>/` |
| `--sections` | comma-separated focus sections |
| `--no-cache` | ignore/skip the on-disk HTML cache in `.cache/` |
| `--no-browser`| disable the Playwright fallback |

## Output schema (`output/<brand>/<id>_<name>.json`)
```json
{
  "source": "gsmarena.com",
  "url": "https://www.gsmarena.com/samsung_galaxy_s26_fe_5g-14870.php",
  "gsmarena_id": 14870,
  "name": "Samsung Galaxy S26 FE",
  "image": "https://.../bigpic.jpg",
  "scraped_at": "2026-09-02T07:45:00+00:00",
  "focus":    { "Network": {...}, "Launch": {...}, "Body": {...}, "Platform": {...}, "Misc": {...} },
  "sections": { "Network": {...}, "Launch": {...}, ...every section on the page... }
}
```
`focus` is a subset of `sections`; both preserve gsmarena's own field labels, and
multi-value fields (e.g. per-region bands) are joined with ` | `.

## Being a good citizen
- Default 2s delay + jitter, on-disk cache so re-runs don't re-hit the site, realistic
  headers, retry/backoff. Raise `--delay` for large families.
- Check gsmarena's Terms of Service before large or repeated crawls; this is for
  personal/research use.

## Extending to more sites
Each site is a fetch + a parser; the `Fetcher` (cache, throttle, browser fallback) is
site-agnostic. gsmarena (`crawler.py`) and mifirm (`mifirm.py`) are implemented and
routed by hostname in `scrape_device()`. To add another, write a `parse_*` returning
the same dict shape and add a branch there + `export.py`.
```
