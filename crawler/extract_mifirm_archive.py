#!/usr/bin/env python3
"""Re-parse the cached mifirm.net HTML into a captured-derived CSV.

WHY THIS EXISTS RATHER THAN A TABLE COPY
docs/LEGACY_IMPORT_POLICY.md is explicit: "No legacy table is copied wholesale."
What it does permit is "captured raw source payloads whose origin and retrieval
time are recoverable". The legacy `roms` table is a parse result; the 338 files
in device-crawler/.cache/ are the payloads. So the import starts from the HTML.

WHY A CSV IN BETWEEN
Mobile Observatory's adapters are stdlib-only -- every one of them reads csv/io
and nothing else. Parsing this HTML needs selectolax, and adding a parser
dependency to the new system to import an old source is the wrong trade. This
script runs ONCE on the old side (which already has selectolax), and the new
side reads a CSV like it does for every other source.

Provenance is carried per row, not asserted in a comment: every row records the
SHA-256 of the HTML file it came from and that file's mtime as retrieved_at, so
any row can be traced back to bytes on disk and re-derived.
"""
from __future__ import annotations

import csv
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/user/work/git/device-crawler")
from mifirm import parse_model  # noqa: E402  (legacy parser, reused deliberately)

CACHE = Path("/home/user/work/git/device-crawler/.cache")
OUT = Path(__file__).resolve().parent / "relay" / "results" / "mifirm-archive"
OUT.mkdir(parents=True, exist_ok=True)
CSV_PATH = OUT / "mifirm-firmware-archive.csv"

FIELDS = ["codename", "device_name", "version", "android", "region", "branch",
          "type", "updated_at", "size", "downloads", "download_url",
          "source_url", "source_sha256", "retrieved_at"]


def main() -> int:
    files = sorted(p for p in CACHE.glob("https___mifirm.net_model_*.html"))
    print(f"  cached model pages: {len(files)}")

    rows, skipped, no_rows = [], [], []
    for path in files:
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        retrieved = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        # reconstruct the URL the file name was derived from
        url = "https://" + path.name[len("https___"):-len(".html")].replace("_", "/", 2)
        try:
            parsed = parse_model(raw.decode("utf-8", "replace"), url)
        except Exception as exc:                      # noqa: BLE001
            skipped.append((path.name, repr(exc)))
            continue
        if not parsed.get("roms"):
            no_rows.append(path.name)
            continue
        for r in parsed["roms"]:
            rows.append({
                "codename": parsed.get("codename") or "",
                "device_name": parsed.get("name") or "",
                "version": r.get("version") or "",
                "android": r.get("android") or "",
                "region": r.get("region") or "",
                "branch": r.get("branch") or "",
                "type": r.get("type") or "",
                "updated_at": r.get("updated_at") or "",
                "size": r.get("size") or "",
                "downloads": r.get("downloads") or "",
                "download_url": r.get("download_url") or "",
                "source_url": url,
                "source_sha256": sha,
                "retrieved_at": retrieved,
            })

    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    print(f"  parsed rows written: {len(rows):,}  ->  {CSV_PATH}")
    print(f"  pages with no rom table: {len(no_rows)}")
    if skipped:
        print(f"  PARSE FAILURES ({len(skipped)}):")
        for n, e in skipped[:5]:
            print(f"    {n}: {e}")

    # Policy step 7: reconcile counts against the legacy parse rather than
    # trusting that a run which printed a number did the right thing.
    from common import connect
    legacy = connect().execute(
        "SELECT COUNT(*) FROM roms WHERE source='mifirm.net'").fetchone()[0]
    print(f"\n  RECONCILE  legacy roms rows {legacy:,}  vs  re-parsed {len(rows):,}"
          f"   delta {len(rows)-legacy:+,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
