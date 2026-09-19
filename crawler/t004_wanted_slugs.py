#!/usr/bin/env python3
"""T004 WANTED-slugs-by-brand: Collect slugs for firmware-atlas priority brands.

Order (per firmware-atlas GO 20260919T200003-104):
  Samsung (209 devices, 16,355 builds)  — 0 slugs, brand new
  Xiaomi  (covers Redmi/Mi/POCO)        — 98/549 slugs, resume
  Tecno   (319 devices, 333 builds)     — 75/195 slugs, resume

Then: spec pages for all newly collected + previously unfetched slugs.

Budget: 1,200/day via host_budget. Hard stop on 429.
One process only. 5s base + 0-3s jitter delay.
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

SPEC_FIELDS = [
    'chipset', 'cpu', 'gpu', 'os', 'wlan', 'bluetooth', 'nfc', 'gps',
    'net2g', 'net3g', 'net4g', 'net5g', 'displaysize', 'internalmemory',
    'batdescription1', 'body-weight', 'released-hl', 'chipset-hl',
]

# WANTED brands, in firmware-atlas priority order
# (brand_name_on_gsmarena, collected_so_far, declared_on_gsmarena_or_0_if_new)
WANTED_BRANDS = [
    ('Samsung', 0, 0),       # brand new — discover page count from makers.php3
    ('Xiaomi', 98, 549),     # resume (covers Redmi, Mi, POCO, Pocophone, Mix)
    ('Tecno', 75, 195),      # resume
]

# Regexes from original collect_gsm_slugs.py
DEV_RE = re.compile(
    r'<a href="([a-z0-9_\-]+-\d+\.php)"[^>]*>.*?<strong><span>(.*?)</span>',
    re.S | re.I)
BRAND_RE = re.compile(
    r'<a href=([a-z0-9_\-]+-phones-\d+\.php)>([^<]{1,40})<br\s*/?>'
    r'\s*<span>\s*(\d+)\s+devices?\s*</span>', re.I)
PAGE_RE = re.compile(r'<a href="([a-z0-9_\-]+-p(\d+)\.php)"', re.I)

stop_flag = False
def _sig(sig, frame):
    global stop_flag
    stop_flag = True
    print("\n[SIGNAL] Saving progress and exiting...")
signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)


def fetch(url):
    """Fetch a URL. Returns (body, status). Hard stop on 429."""
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


def polite_delay(count):
    """5s base + 0-3s jitter, 60s cooldown every 50 pages."""
    if count > 0 and count % 50 == 0:
        print(f"    [{count} requests] cooldown 60s...")
        time.sleep(60)
    else:
        time.sleep(5.0 + random.uniform(0, 3.0))


def is_device_slug(slug):
    """True if slug is a device spec page, not a brand listing page."""
    return bool(re.search(r'-\d+\.php$', slug)) and '-phones-f-' not in slug


def devices_on_page(html):
    """Extract device slugs from a brand listing page."""
    out = []
    for slug, name in DEV_RE.findall(html):
        name = re.sub(r"<[^>]+>", "", name)
        name = re.sub(r"\s+", " ", name).strip()
        if name and slug:
            out.append((name, slug))
    return out


def load_existing_slugs():
    """Load existing slug CSV into set of slugs."""
    slugs = set()
    if os.path.exists(SLUGS_CSV):
        with open(SLUGS_CSV, newline='') as f:
            for row in csv.DictReader(f):
                slugs.add(row['slug'])
    return slugs


def append_slug_row(brand, name, slug):
    """Append a new slug to the CSV."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(SLUGS_CSV, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['brand', 'device', 'slug', 'fetched_at'])
        w.writerow({'brand': brand, 'device': name, 'slug': slug, 'fetched_at': now})


def extract_specs(html):
    """Extract spec fields from a gsmarena spec page."""
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


def discover_brands():
    """Fetch makers.php3 to find brand URLs."""
    print("Fetching makers.php3...")
    html, status = fetch(f"{BASE}/makers.php3")
    if status == 429:
        return None
    if not html:
        print(f"  makers.php3 returned {status}")
        return None

    brands = {}
    for bslug, bname, bdeclared in BRAND_RE.findall(html):
        brands[bname.strip()] = (bslug, int(bdeclared))
    print(f"  Found {len(brands)} brands")
    return brands


def collect_brand_slugs(brand_name, brand_url_slug, existing, collected_so_far):
    """Collect all device slugs for a brand. Returns count of new slugs."""
    global stop_flag

    stem = brand_url_slug.replace('.php', '')
    # Extract brand ID for pagination URL construction
    m = re.match(r'(.+-phones)-(\d+)', stem)
    if not m:
        print(f"    Cannot parse brand slug: {brand_url_slug}")
        return 0

    brand_base = m.group(1)
    brand_id = m.group(2)

    # Determine start page
    if collected_so_far == 0:
        start_page = 1
    else:
        start_page = max(2, (collected_so_far // 50) + 1)

    print(f"  {brand_name}: {collected_so_far} existing, starting from page {start_page}")

    total_new = 0
    req_count = 0

    for page_num in range(start_page, 200):  # safety limit
        if stop_flag:
            break

        if page_num == 1:
            url = f"{BASE}/{brand_url_slug}"
        else:
            url = f"{BASE}/{brand_base}-f-{brand_id}-0-p{page_num}.php"

        html, status = fetch(url)
        req_count += 1

        if status == 429:
            stop_flag = True
            break
        if status == 'budget':
            stop_flag = True
            break
        if status == 404 or not html:
            # End of pages for this brand
            if page_num > 1:
                print(f"    page {page_num}: end of brand (status={status})")
            break

        devs = devices_on_page(html)
        if not devs:
            print(f"    page {page_num}: 0 devices — end of brand")
            break

        new_on_page = 0
        for name, dslug in devs:
            if dslug not in existing:
                existing.add(dslug)
                append_slug_row(brand_name, name, dslug)
                new_on_page += 1
                total_new += 1

        print(f"    page {page_num}: {len(devs)} listed, {new_on_page} new")

        polite_delay(req_count)

    return total_new


def phase_slugs():
    """Phase 1: Collect slugs for WANTED brands."""
    global stop_flag

    print("=" * 60)
    print("PHASE 1: WANTED Brand Slug Collection")
    print("=" * 60)

    existing = load_existing_slugs()
    print(f"Existing slugs in CSV: {len(existing)}")

    # Discover brand URLs from makers.php3
    brands = discover_brands()
    if brands is None:
        stop_flag = True
        return 0

    polite_delay(1)

    total_new = 0
    for brand_name, collected_so_far, declared in WANTED_BRANDS:
        if stop_flag:
            break

        if brand_name not in brands:
            print(f"  {brand_name}: NOT FOUND on gsmarena — skip")
            continue

        brand_slug, gsm_declared = brands[brand_name]
        actual_collected = collected_so_far
        # Update declared count from live gsmarena data
        print(f"\n  {brand_name}: gsmarena declares {gsm_declared} devices (brand slug: {brand_slug})")

        new = collect_brand_slugs(brand_name, brand_slug, existing, actual_collected)
        total_new += new

        if new:
            print(f"  → {brand_name}: +{new} new slugs")

    print(f"\nPhase 1 complete: +{total_new} new slugs, total in CSV: {len(existing)}")
    return total_new


def phase_specs():
    """Phase 2: Fetch spec pages for all unfetched slugs in priority brands."""
    global stop_flag

    print("\n" + "=" * 60)
    print("PHASE 2: Spec Pages for WANTED Brands")
    print("=" * 60)

    # Load all slugs from CSV
    brand_slugs = {}  # brand -> [(device, slug)]
    if os.path.exists(SLUGS_CSV):
        with open(SLUGS_CSV, newline='') as f:
            for row in csv.DictReader(f):
                b = row['brand']
                brand_slugs.setdefault(b, []).append((row['device'], row['slug']))

    # Wanted brands in priority order
    wanted_gsm_brands = ['Samsung', 'Xiaomi', 'Tecno']

    fetched = load_specs_progress()
    print(f"Already have spec pages: {len(fetched)}")

    # Build fetch queue: wanted brands first
    queue = []
    for brand in wanted_gsm_brands:
        if brand in brand_slugs:
            for device, slug in brand_slugs[brand]:
                if slug not in fetched and is_device_slug(slug):
                    queue.append((brand, device, slug))

    print(f"Spec pages to fetch for WANTED brands: {len(queue)}")
    if not queue:
        print("  Nothing to fetch!")
        return

    count = 0
    for brand, device, slug in queue:
        if stop_flag:
            break

        url = f"{BASE}/{slug}"
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
                'brand': brand,
                'device': device,
                'slug': slug,
                'fetched_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            }
            spec_row.update(specs)
            append_spec_row(spec_row)
            fetched.add(slug)

            chipset = specs.get('chipset', specs.get('chipset-hl', '-'))
            if count <= 5 or count % 25 == 0:
                print(f"  [{count}] {brand}/{device}: {chipset[:60]}")
        elif status == 404:
            fetched.add(slug)
            if count <= 5 or count % 25 == 0:
                print(f"  [{count}] {brand}/{device}: 404")
        else:
            print(f"  [{count}] {brand}/{device}: status={status}, skipping")

        if count % 25 == 0:
            save_specs_progress(fetched)

        polite_delay(count)

    save_specs_progress(fetched)
    print(f"\nSpec pages fetched this run: {count}")
    print(f"Total spec pages: {len(fetched)}")


def main():
    global stop_flag

    _, spent, limit = host_budget('www.gsmarena.com', spend=0, limit=DAILY_LIMIT)
    print(f"Budget: {spent}/{limit} used today")
    print(f"Available: {limit - spent} requests\n")

    # Phase 1: Slug collection
    phase_slugs()

    # Phase 2: Spec pages (only if budget remains and no 429)
    if not stop_flag:
        _, spent, _ = host_budget('www.gsmarena.com', spend=0)
        remaining = DAILY_LIMIT - spent
        if remaining > 10:
            print(f"\nBudget remaining: {remaining} — continuing to spec pages")
            phase_specs()
        else:
            print(f"\nBudget remaining: {remaining} — deferring spec pages")

    # Final report
    _, spent, _ = host_budget('www.gsmarena.com', spend=0)
    print(f"\n{'=' * 60}")
    print(f"FINAL REPORT")
    print(f"{'=' * 60}")
    print(f"Budget used: {spent}/{limit}")

    if os.path.exists(SPECS_CSV):
        with open(SPECS_CSV) as f:
            spec_count = sum(1 for _ in f) - 1
        print(f"Spec pages total: {spec_count}")

    if os.path.exists(SLUGS_CSV):
        with open(SLUGS_CSV) as f:
            slug_count = sum(1 for _ in f) - 1
        # Brand breakdown
        brand_counts = {}
        with open(SLUGS_CSV, newline='') as f:
            for row in csv.DictReader(f):
                b = row['brand']
                brand_counts[b] = brand_counts.get(b, 0) + 1
        print(f"Slugs total: {slug_count}")
        for b in ['Samsung', 'Xiaomi', 'Tecno']:
            print(f"  {b}: {brand_counts.get(b, 0)} slugs")

    if stop_flag:
        print("\n⚠ Stopped early (429 / SIGTERM / budget)")


if __name__ == '__main__':
    main()
