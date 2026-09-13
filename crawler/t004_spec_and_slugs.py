#!/usr/bin/env python3
"""T004 Half 2: Spec page fetcher + slug collection resume.

Priority (per firmware-atlas):
1. Spec pages for existing corpus slugs (Xiaomi, TECNO first)
2. Resume slug collection for 28 incomplete brands

Budget: 1,200/day via host_budget. Hard stop on 429.
"""

import csv, json, os, random, re, sys, time, signal, urllib.request, urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import host_budget, USER_AGENT

BASE = "https://www.gsmarena.com"
RESULTS = os.path.join(os.path.dirname(__file__), "relay", "results", "T004-gsmarena-slugs")
SLUGS_CSV = os.path.join(RESULTS, "gsm_slugs.csv")
SPECS_CSV = os.path.join(RESULTS, "gsm_specs.csv")
SPECS_PROGRESS = os.path.join(RESULTS, "specs_progress.json")
DAILY_LIMIT = 1200

# Spec fields to extract from data-spec attributes
SPEC_FIELDS = [
    'chipset', 'cpu', 'gpu', 'os', 'wlan', 'bluetooth', 'nfc', 'gps',
    'net2g', 'net3g', 'net4g', 'net5g', 'displaysize', 'internalmemory',
    'batdescription1', 'body-weight', 'released-hl', 'chipset-hl',
]

# Brand priority for spec fetching (corpus-heavy first)
PRIORITY_BRANDS = ['Xiaomi', 'Tecno', 'Honor', 'vivo', 'Realme', 'OnePlus',
                   'Nokia', 'Asus', 'HTC', 'LG', 'Apple', 'Blackview',
                   'Oukitel', 'Micromax', 'Sony Ericsson', 'Panasonic']

# Incomplete brands for slug resume (from resume-point.txt)
INCOMPLETE_BRANDS = {
    'alcatel': (63, 414), 'Amazon': (14, 25), 'Apple': (80, 152),
    'Asus': (82, 208), 'Bird': (60, 61), 'Blackview': (100, 138),
    'Celkon': (94, 229), 'Coolpad': (56, 57), 'Fairphone': (5, 6),
    'Honor': (110, 337), 'HTC': (94, 297), 'Intex': (14, 15),
    'LG': (95, 667), 'Micromax': (101, 289), 'Nokia': (88, 598),
    'OnePlus': (80, 115), 'Oukitel': (102, 121), 'Panasonic': (105, 123),
    'Plum': (99, 113), 'Prestigio': (53, 56), 'Realme': (82, 298),
    'Sagem': (104, 120), 'Sony Ericsson': (108, 188), 'T-Mobile': (66, 69),
    'Tecno': (75, 195), 'vivo': (110, 616), 'Xiaomi': (98, 549),
    'Yezz': (99, 113),
}

# gsmarena brand slug mapping (brand name -> URL slug)
BRAND_SLUGS = {
    'alcatel': 'alcatel-phones',
    'Amazon': 'amazon-phones',
    'Apple': 'apple-phones',
    'Asus': 'asus-phones',
    'Bird': 'bird-phones',
    'Blackview': 'blackview-phones',
    'Celkon': 'celkon-phones',
    'Coolpad': 'coolpad-phones',
    'Fairphone': 'fairphone-phones',
    'Honor': 'honor-phones',
    'HTC': 'htc-phones',
    'Intex': 'intex-phones',
    'LG': 'lg-phones',
    'Micromax': 'micromax-phones',
    'Nokia': 'nokia-phones',
    'OnePlus': 'oneplus-phones',
    'Oukitel': 'oukitel-phones',
    'Panasonic': 'panasonic-phones',
    'Plum': 'plum-phones',
    'Prestigio': 'prestigio-phones',
    'Realme': 'realme-phones',
    'Sagem': 'sagem-phones',
    'Sony Ericsson': 'sony_ericsson-phones',
    'T-Mobile': 't_mobile-phones',
    'Tecno': 'tecno-phones',
    'vivo': 'vivo-phones',
    'Xiaomi': 'xiaomi-phones',
    'Yezz': 'yezz-phones',
}

stop_flag = False
def _sigterm(sig, frame):
    global stop_flag
    stop_flag = True
    print("\n[SIGTERM] Saving progress and exiting...")
signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT, _sigterm)


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


def is_device_slug(slug):
    """True if slug is a device spec page, not a brand listing page."""
    return bool(re.search(r'-\d+\.php$', slug)) and '-phones-f-' not in slug


def extract_specs(html):
    """Extract spec fields from a gsmarena spec page."""
    specs = {}

    # Extract device name
    name_m = re.search(r'<h1[^>]*class="specs-phone-name-title"[^>]*>(.*?)</h1>', html)
    if name_m:
        specs['device_name'] = re.sub(r'<[^>]+>', '', name_m.group(1)).strip()

    # Extract all data-spec values — match up to closing </td> for full content
    for m in re.finditer(r'data-spec="([^"]+)"[^>]*>(.*?)</td>', html, re.DOTALL):
        key, val = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        val = re.sub(r'\s+', ' ', val).strip()
        # Truncate overly long values (some have embedded JS)
        if len(val) > 300:
            val = val[:300]
        if key in SPEC_FIELDS and val:
            specs[key] = val

    # Fallback: also try </span> boundary for fields not yet found
    for m in re.finditer(r'data-spec="([^"]+)"[^>]*>(.*?)</span>', html, re.DOTALL):
        key, val = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        val = re.sub(r'\s+', ' ', val).strip()
        if len(val) > 300:
            val = val[:300]
        if key in SPEC_FIELDS and val and key not in specs:
            specs[key] = val

    return specs


def load_existing_slugs():
    """Load existing slug CSV into a dict keyed by slug."""
    slugs = {}
    if os.path.exists(SLUGS_CSV):
        with open(SLUGS_CSV, newline='') as f:
            for row in csv.DictReader(f):
                slugs[row['slug']] = row
    return slugs


def load_specs_progress():
    """Load set of already-fetched spec slugs."""
    if os.path.exists(SPECS_PROGRESS):
        with open(SPECS_PROGRESS) as f:
            return set(json.load(f).get('fetched', []))
    return set()


def save_specs_progress(fetched_set):
    """Save progress atomically."""
    tmp = SPECS_PROGRESS + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'fetched': sorted(fetched_set), 'updated': datetime.now(timezone.utc).isoformat()}, f)
    os.replace(tmp, SPECS_PROGRESS)


def append_spec_row(row, write_header=False):
    """Append a row to specs CSV."""
    fieldnames = ['brand', 'device', 'slug', 'device_name',
                  'chipset', 'chipset-hl', 'cpu', 'gpu', 'os',
                  'wlan', 'bluetooth', 'nfc', 'gps',
                  'net2g', 'net3g', 'net4g', 'net5g',
                  'displaysize', 'internalmemory', 'batdescription1',
                  'body-weight', 'released-hl', 'fetched_at']
    mode = 'a' if not write_header else 'w'
    with open(SPECS_CSV, mode, newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        if write_header:
            w.writeheader()
        w.writerow(row)


def phase1_spec_pages():
    """Fetch spec pages for existing slugs, priority brands first."""
    global stop_flag
    
    slugs = load_existing_slugs()
    fetched = load_specs_progress()
    
    print(f"=== Phase 1: Spec Pages ===")
    print(f"  Total slugs: {len(slugs)}")
    print(f"  Already fetched: {len(fetched)}")
    
    # If no specs CSV yet, write header only
    if not os.path.exists(SPECS_CSV):
        fieldnames = ['brand', 'device', 'slug', 'device_name',
                      'chipset', 'chipset-hl', 'cpu', 'gpu', 'os',
                      'wlan', 'bluetooth', 'nfc', 'gps',
                      'net2g', 'net3g', 'net4g', 'net5g',
                      'displaysize', 'internalmemory', 'batdescription1',
                      'body-weight', 'released-hl', 'fetched_at']
        with open(SPECS_CSV, 'w', newline='') as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()
    
    # Build ordered slug list (priority brands first)
    by_brand = {}
    for slug, row in slugs.items():
        b = row['brand']
        by_brand.setdefault(b, []).append(row)
    
    ordered = []
    seen_brands = set()
    for b in PRIORITY_BRANDS:
        if b in by_brand:
            ordered.extend(by_brand[b])
            seen_brands.add(b)
    for b in sorted(by_brand):
        if b not in seen_brands:
            ordered.extend(by_brand[b])
    
    # Filter out already fetched and non-device slugs (listing pages)
    todo = [r for r in ordered if r['slug'] not in fetched and is_device_slug(r['slug'])]
    skipped = len(ordered) - len([r for r in ordered if is_device_slug(r['slug'])])
    if skipped:
        print(f"  Skipped {skipped} non-device slugs (listing pages)")
    print(f"  To fetch: {len(todo)}")
    
    count = 0
    for row in todo:
        if stop_flag:
            break
        
        url = f"{BASE}/{row['slug']}"
        html, status = fetch(url)
        
        if status == 429:
            stop_flag = True
            break
        if status == 'budget':
            break
        
        if html:
            specs = extract_specs(html)
            spec_row = {
                'brand': row['brand'],
                'device': row['device'],
                'slug': row['slug'],
                'fetched_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            }
            spec_row.update(specs)
            append_spec_row(spec_row)
            fetched.add(row['slug'])
            count += 1
            
            chipset = specs.get('chipset', specs.get('chipset-hl', '-'))
            if count % 10 == 0 or count <= 3:
                print(f"  [{count}] {row['brand']}/{row['device']}: {chipset[:60]}")
        elif status == 404:
            # Mark as fetched so we don't retry
            fetched.add(row['slug'])
            count += 1
            if count % 10 == 0:
                print(f"  [{count}] {row['brand']}/{row['device']}: 404")
        else:
            # Transient error, skip but don't mark
            print(f"  [{count}] {row['brand']}/{row['device']}: status={status}, skipping")
        
        # Save progress every 25 pages
        if count % 25 == 0:
            save_specs_progress(fetched)
        
        # Polite delay: 5s base + 0-3s jitter, 60s cooldown every 50 pages
        delay = 5.0 + random.uniform(0, 3.0)
        if count % 50 == 0 and count > 0:
            print(f"  [{count}] cooldown 60s...")
            time.sleep(60)
        else:
            time.sleep(delay)
    
    save_specs_progress(fetched)
    print(f"  Spec pages fetched this run: {count}")
    print(f"  Total spec pages: {len(fetched)}")
    return not stop_flag


  # Same regexes as the original collect_gsm_slugs.py
DEV_RE = re.compile(r'<a href="([a-z0-9_\-]+-\d+\.php)"[^>]*>.*?<strong><span>(.*?)</span>',
                    re.S | re.I)
BRAND_RE = re.compile(r'<a href=([a-z0-9_\-]+-phones-\d+\.php)>([^<]{1,40})<br\s*/?>'
                      r'\s*<span>\s*(\d+)\s+devices?\s*</span>', re.I)
PAGE_RE = re.compile(r'<a href="([a-z0-9_\-]+-p(\d+)\.php)"', re.I)


def devices_on_page(html):
    """Extract device slugs from a brand listing page (same as original crawler)."""
    out = []
    for slug, name in DEV_RE.findall(html):
        name = re.sub(r"<[^>]+>", "", name)
        name = re.sub(r"\s+", " ", name).strip()
        if name and slug:
            out.append((name, slug))
    return out


def phase2_slug_collection():
    """Resume slug collection for incomplete brands using makers.php3."""
    global stop_flag

    print(f"\n=== Phase 2: Slug Collection ===")

    existing_slugs = load_existing_slugs()
    existing_set = set(existing_slugs.keys())
    print(f"  Existing slugs: {len(existing_set)}")

    # Step 1: Fetch makers.php3 to get brand URLs with IDs
    print("  Fetching makers.php3...")
    html, status = fetch(f"{BASE}/makers.php3")
    if status == 429:
        stop_flag = True
        return
    if not html:
        print(f"  makers.php3 returned {status} — cannot continue slug collection")
        return

    brands_found = BRAND_RE.findall(html)
    print(f"  Found {len(brands_found)} brands on makers.php3")

    # Map brand names to their URLs
    brand_url_map = {}
    for bslug, bname, bdeclared in brands_found:
        bname = bname.strip()
        brand_url_map[bname] = (bslug, int(bdeclared))

    time.sleep(1.5)

    # Sort incomplete brands: near-complete first (fewest remaining)
    brands_by_remaining = []
    for brand, (collected, declared) in INCOMPLETE_BRANDS.items():
        remaining = declared - collected
        if brand in brand_url_map:
            brands_by_remaining.append((remaining, brand, collected, declared))
        else:
            print(f"  {brand}: not found in makers.php3, skip")
    brands_by_remaining.sort()

    total_new = 0
    for remaining, brand, collected, declared in brands_by_remaining:
        if stop_flag:
            break

        brand_page_slug, _ = brand_url_map[brand]
        # Calculate start page: original crawler uses ~50 per page
        start_page = max(2, (collected // 50) + 1)

        print(f"  {brand}: {collected}/{declared} — resume from ~page {start_page}")

        # Walk pages by visiting page 1 first (we need its pagination links)
        # But to save budget, try constructing page URLs directly
        # Pattern from original: brand slug like "xiaomi-phones-80.php"
        # Pagination: links ending in -p{N}.php
        done_pages = set()
        brand_new = 0

        # Start with page 1 URL to find pagination structure
        page1_url = f"{BASE}/{brand_page_slug}"

        # We need to get the page URL stem. Brand slug: "xiaomi-phones-80.php"
        # Remove .php, that's the stem: "xiaomi-phones-80"
        stem = brand_page_slug.replace('.php', '')

        # For page N, try: {stem}-p{N}.php (as found by PAGE_RE in listing HTML)
        # But first, let's visit page 1 to confirm pagination exists and find the pattern
        if start_page <= 2:
            # Need to fetch page 1 to find pagination links
            page1_html, st = fetch(page1_url)
            if st == 429:
                stop_flag = True
                break
            if not page1_html:
                print(f"    page 1: {st} — skip brand")
                continue

            done_pages.add(0)
            # Collect devices from page 1
            for name, dslug in devices_on_page(page1_html):
                if dslug not in existing_set:
                    existing_set.add(dslug)
                    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                    row = {'brand': brand, 'device': name, 'slug': dslug, 'fetched_at': now}
                    with open(SLUGS_CSV, 'a', newline='') as f:
                        w = csv.DictWriter(f, fieldnames=['brand', 'device', 'slug', 'fetched_at'])
                        w.writerow(row)
                    brand_new += 1
                    total_new += 1

            # Find all pagination links
            page_links = PAGE_RE.findall(page1_html)
            time.sleep(5.0 + random.uniform(0, 3.0))
        else:
            page_links = []

        # Compute pages to visit
        # The page URLs follow: {stem}-f-{id}-0-p{N}.php or {stem}-p{N}.php
        # Try to discover the pattern from pagination links found
        page_url_template = None
        for href, num in page_links:
            # Extract the template by replacing the page number
            template = href[:href.rfind('-p')] + '-p{}.php'
            page_url_template = template
            break

        if not page_url_template:
            # Construct from brand slug: e.g. "xiaomi-phones-80" -> "xiaomi-phones-f-80-0-p{}.php"
            # Extract brand_id from the slug
            m = re.match(r'(.+-phones)-(\d+)', stem)
            if m:
                page_url_template = f"{m.group(1)}-f-{m.group(2)}-0-p{{}}.php"
            else:
                print(f"    Cannot construct page URLs for {brand}, skip")
                continue

        # Now walk from start_page onwards
        for page_num in range(start_page, 100):  # safety limit
            if stop_flag:
                break
            if page_num in done_pages:
                continue

            page_url = f"{BASE}/{page_url_template.format(page_num)}"
            html, st = fetch(page_url)
            if st == 429:
                stop_flag = True
                break
            if st == 'budget':
                stop_flag = True
                break
            if st == 404 or not html:
                print(f"    page {page_num}: {st} — end of brand")
                break

            done_pages.add(page_num)
            devs = devices_on_page(html)
            new_on_page = 0
            now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            for name, dslug in devs:
                if dslug not in existing_set:
                    existing_set.add(dslug)
                    row = {'brand': brand, 'device': name, 'slug': dslug, 'fetched_at': now}
                    with open(SLUGS_CSV, 'a', newline='') as f:
                        w = csv.DictWriter(f, fieldnames=['brand', 'device', 'slug', 'fetched_at'])
                        w.writerow(row)
                    new_on_page += 1
                    brand_new += 1
                    total_new += 1

            print(f"    page {page_num}: {len(devs)} listed, {new_on_page} new")

            if not devs:
                break
            time.sleep(5.0 + random.uniform(0, 3.0))

        if brand_new:
            print(f"    → {brand}: +{brand_new} new slugs")

    print(f"  Total new slugs: {total_new}")
    print(f"  Grand total: {len(existing_set)}")


def main():
    global stop_flag
    
    _, spent, limit = host_budget('www.gsmarena.com', spend=0, limit=DAILY_LIMIT)
    print(f"Budget: {spent}/{limit} used today")
    print(f"Available: {limit - spent} requests")
    print()
    
    # Phase 1: Spec pages
    has_budget = phase1_spec_pages()
    
    # Phase 2: Slug collection (only if budget remains)
    if has_budget and not stop_flag:
        _, spent, _ = host_budget('www.gsmarena.com', spend=0)
        remaining = DAILY_LIMIT - spent
        if remaining > 10:
            print(f"\n  Budget remaining: {remaining} — continuing to slug collection")
            phase2_slug_collection()
        else:
            print(f"\n  Budget remaining: {remaining} — skipping slug collection")
    
    # Final budget report
    _, spent, _ = host_budget('www.gsmarena.com', spend=0)
    print(f"\n=== Final ===")
    print(f"Budget used: {spent}/{limit}")
    
    # Count specs
    if os.path.exists(SPECS_CSV):
        with open(SPECS_CSV) as f:
            spec_count = sum(1 for _ in f) - 1
        print(f"Spec pages total: {spec_count}")
    
    # Count slugs
    with open(SLUGS_CSV) as f:
        slug_count = sum(1 for _ in f) - 1
    print(f"Slugs total: {slug_count}")
    
    if stop_flag:
        print("\n⚠ Stopped early (429 / SIGTERM / budget)")


if __name__ == '__main__':
    main()
