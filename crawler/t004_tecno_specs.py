#!/usr/bin/env python3
"""T004 Tecno spec pages: 192 slugs, ~100/day, newest first.

Per firmware-atlas GO 20260923T155218-912.
Stop on first 429. Hard cap at 100 pages per run.
"""

import csv, json, os, random, re, sys, time, signal
import urllib.request, urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import host_budget, USER_AGENT

BASE = "https://www.gsmarena.com"
RESULTS = os.path.join(os.path.dirname(__file__), "relay", "results", "T004-gsmarena-slugs")
SLUGS_CSV = os.path.join(RESULTS, "gsm_slugs.csv")
SPECS_CSV = os.path.join(RESULTS, "gsm_specs.csv")
SPECS_PROGRESS = os.path.join(RESULTS, "specs_progress.json")
DAILY_LIMIT = 1200
RUN_CAP = 100  # ~100 pages/day to preserve the channel

SPEC_FIELDS = [
    'chipset', 'cpu', 'gpu', 'os', 'wlan', 'bluetooth', 'nfc', 'gps',
    'net2g', 'net3g', 'net4g', 'net5g', 'displaysize', 'internalmemory',
    'batdescription1', 'body-weight', 'released-hl', 'chipset-hl',
]

stop_flag = False
def _sig(sig, frame):
    global stop_flag
    stop_flag = True
    print("\n[SIGNAL] Saving progress and exiting...")
signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)


def fetch(url):
    ok, spent, limit = host_budget('www.gsmarena.com', spend=1, limit=DAILY_LIMIT)
    if not ok:
        print(f"  BUDGET EXHAUSTED ({spent}/{limit})")
        return None, 'budget'
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.read().decode('utf-8', errors='replace'), r.status
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = e.headers.get('Retry-After', 'not set')
            print(f"  429 — HARD STOP (Retry-After: {retry_after})")
            return None, 429
        elif e.code == 404:
            return None, 404
        else:
            print(f"  HTTP {e.code}")
            return None, e.code
    except Exception as e:
        print(f"  Error: {e}")
        return None, 'error'


def extract_specs(html):
    specs = {}
    name_m = re.search(r'<h1[^>]*class="specs-phone-name-title"[^>]*>(.*?)</h1>', html)
    if name_m:
        specs['device_name'] = re.sub(r'<[^>]+>', '', name_m.group(1)).strip()
    for m in re.finditer(r'data-spec="([^"]+)"[^>]*>(.*?)</td>', html, re.DOTALL):
        key, val = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        val = re.sub(r'\s+', ' ', val).strip()
        if len(val) > 300: val = val[:300]
        if key in SPEC_FIELDS and val:
            specs[key] = val
    for m in re.finditer(r'data-spec="([^"]+)"[^>]*>(.*?)</span>', html, re.DOTALL):
        key, val = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        val = re.sub(r'\s+', ' ', val).strip()
        if len(val) > 300: val = val[:300]
        if key in SPEC_FIELDS and val and key not in specs:
            specs[key] = val
    return specs


def load_specs_progress():
    if os.path.exists(SPECS_PROGRESS):
        with open(SPECS_PROGRESS) as f:
            return set(json.load(f).get('fetched', []))
    return set()


def save_specs_progress(fetched_set):
    tmp = SPECS_PROGRESS + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'fetched': sorted(fetched_set),
                   'updated': datetime.now(timezone.utc).isoformat()}, f)
    os.replace(tmp, SPECS_PROGRESS)


def is_device_slug(slug):
    return bool(re.search(r'-\d+\.php$', slug)) and '-phones-f-' not in slug


def append_spec_row(row):
    fieldnames = ['brand', 'device', 'slug', 'device_name',
                  'chipset', 'chipset-hl', 'cpu', 'gpu', 'os',
                  'wlan', 'bluetooth', 'nfc', 'gps',
                  'net2g', 'net3g', 'net4g', 'net5g',
                  'displaysize', 'internalmemory', 'batdescription1',
                  'body-weight', 'released-hl', 'fetched_at']
    with open(SPECS_CSV, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        w.writerow(row)


def main():
    global stop_flag

    _, spent, limit = host_budget('www.gsmarena.com', spend=0, limit=DAILY_LIMIT)
    print(f"Budget: {spent}/{limit} used today, {limit - spent} remaining")
    print(f"Run cap: {RUN_CAP} pages\n")

    fetched = load_specs_progress()
    print(f"Spec pages already fetched: {len(fetched)}")

    # Load Tecno slugs, sort newest first (highest slug number = newest)
    tecno = []
    with open(SLUGS_CSV, newline='') as f:
        for r in csv.DictReader(f):
            if r['brand'] == 'Tecno' and r['slug'] not in fetched and is_device_slug(r['slug']):
                tecno.append(r)

    # Sort newest first by extracting the numeric ID from slug
    def slug_id(r):
        m = re.search(r'-(\d+)\.php$', r['slug'])
        return int(m.group(1)) if m else 0
    tecno.sort(key=slug_id, reverse=True)

    print(f"Tecno spec pages to fetch: {len(tecno)}")
    if not tecno:
        print("Nothing to do!")
        return

    print(f"\n{'='*60}")
    print(f"TECNO Spec Pages (newest first, cap {RUN_CAP})")
    print(f"{'='*60}")

    count = 0
    chipset_count = 0
    no_chipset = []

    for r in tecno:
        if stop_flag or count >= RUN_CAP:
            break

        url = f"{BASE}/{r['slug']}"
        html, status = fetch(url)

        if status == 429:
            stop_flag = True
            break
        if status == 'budget':
            stop_flag = True
            break

        count += 1

        if html:
            specs = extract_specs(html)
            spec_row = {
                'brand': 'Tecno', 'device': r['device'], 'slug': r['slug'],
                'fetched_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            }
            spec_row.update(specs)
            append_spec_row(spec_row)
            fetched.add(r['slug'])

            chipset = specs.get('chipset', specs.get('chipset-hl', ''))
            has_chipset = bool(chipset)
            if has_chipset:
                chipset_count += 1
            else:
                no_chipset.append(r['device'])

            if count <= 5 or count % 25 == 0:
                display = specs.get('displaysize', '-')
                os_val = specs.get('os', '-')
                c_short = chipset[:50] if chipset else 'NO CHIPSET'
                print(f"  [{count}] {r['device']}: {c_short}")
                if count <= 5:
                    print(f"        display={display[:40]}  os={os_val[:30]}")
        elif status == 404:
            fetched.add(r['slug'])
            if count <= 5 or count % 10 == 0:
                print(f"  [{count}] {r['device']}: 404")
        else:
            print(f"  [{count}] {r['device']}: status={status}, skip")

        if count % 25 == 0:
            save_specs_progress(fetched)

        # Polite delay: 5s + 0-3s jitter
        time.sleep(5.0 + random.uniform(0, 3.0))

    save_specs_progress(fetched)

    # Report
    print(f"\n{'='*60}")
    print(f"REPORT")
    print(f"{'='*60}")
    _, spent_now, _ = host_budget('www.gsmarena.com', spend=0)
    print(f"Pages fetched this run: {count}")
    print(f"  With chipset: {chipset_count}")
    print(f"  Without chipset: {len(no_chipset)}")
    print(f"Budget used today: {spent_now}/{limit}")
    print(f"Total spec pages: {len(fetched)}")
    print(f"Tecno remaining: {len(tecno) - count}")

    if no_chipset:
        print(f"\nMISSES (page fetched, no chipset):")
        for d in no_chipset:
            print(f"  {d}")

    if stop_flag:
        print("\n!! Stopped early (429 / SIGTERM / budget)")
    elif count >= RUN_CAP:
        print(f"\n-- Run cap reached ({RUN_CAP}). Resume tomorrow.")


if __name__ == '__main__':
    main()
