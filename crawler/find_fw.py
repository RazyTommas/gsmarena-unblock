#!/usr/bin/env python3
"""
find_fw.py — deep search for a specific firmware, on demand.

Give it a device / model / version / region / Android and it searches BOTH the
indexed corpus (data/devices.db, ~46k builds across mifirm / samfw / firmwarefile /
romprovider / needrom / archive.org) AND live archive.org, then prints every match
with its download link and how "gettable" it is:

  DIRECT      no login, fetchable now (archive.org, mifirm)
  MIRROR      external host (Google Drive / MediaFire) — open the link
  BROWSER     needs the real-browser ingest (samfw: Cloudflare)
  LOGIN       needs your account (needrom)  ← you sign in, then it's fetchable
  PAGE        a click-gated download page (gofirmware)

Usage:
  python find_fw.py "Tecno Camon 40"
  python find_fw.py "Galaxy S25 Ultra ILO"          # device + zone
  python find_fw.py "SM-A566B A566BXXS3"            # a specific version string
  python find_fw.py "camon" --android 15 --region MID
  python find_fw.py "Tecno Spark KA9" --download     # fetch the DIRECT (no-login) hits
  python find_fw.py "phantom" --download --out ~/fw  # download dir
"""
from __future__ import annotations
import argparse, json, os, re, sqlite3, urllib.parse, urllib.request
from pathlib import Path

DB = Path(__file__).with_name("data") / "devices.db"
GATE = {  # source -> (label, directly-fetchable?)
    "archive.org": ("DIRECT", True), "mifirm.net": ("DIRECT", True),
    "firmwarefile.com": ("MIRROR", False), "romprovider.com": ("MIRROR", False),
    "samfw.com": ("BROWSER", False), "needrom.com": ("LOGIN", False),
    "gofirmware.com": ("PAGE", False),
}


def search_corpus(terms, android, region):
    if not DB.exists():
        return []
    con = sqlite3.connect(DB)
    cols = [d[0] for d in con.execute("SELECT * FROM roms LIMIT 1").description]
    rows = []
    for r in con.execute("SELECT * FROM roms"):
        d = dict(zip(cols, r))
        hay = " ".join(str(d.get(k) or "") for k in
                       ("source", "device", "model", "codename", "region",
                        "type", "branch", "version", "android")).lower()
        if not all(t in hay for t in terms):
            continue
        if android and str(d.get("android") or "") != str(android):
            continue
        if region and (d.get("region") or "").upper() != region.upper():
            continue
        rows.append(d)
    con.close()
    return rows


def search_archive_org(query, limit=15):
    q = urllib.parse.quote(f"title:({query}) AND (firmware OR stock OR rom OR flash OR MT6)")
    url = (f"https://archive.org/advancedsearch.php?q={q}"
           f"&fl[]=identifier&fl[]=title&rows={limit}&output=json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        docs = json.loads(urllib.request.urlopen(req, timeout=25).read())["response"]["docs"]
    except Exception as e:
        print(f"  (archive.org query failed: {e})")
        return []
    out = []
    for d in docs:
        ident = d["identifier"]
        try:
            req = urllib.request.Request(f"https://archive.org/metadata/{ident}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            meta = json.loads(urllib.request.urlopen(req, timeout=25).read())
            files = [f for f in meta.get("files", [])
                     if f["name"].lower().endswith((".zip", ".rar", ".gz", ".7z"))]
            if not files:
                continue
            big = max(files, key=lambda f: int(f.get("size", 0) or 0))
            mb = int(big.get("size", 0) or 0) / 1e6
            if mb < 40:
                continue
            out.append({"source": "archive.org", "device": d.get("title", "")[:50],
                        "version": big["name"], "size": f"{mb:.0f}MB",
                        "download_url": f"https://archive.org/download/{ident}/"
                                        + urllib.parse.quote(big["name"])})
        except Exception:
            pass
    return out


def download(url, out_dir):
    out_dir = Path(os.path.expanduser(out_dir)); out_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", urllib.parse.unquote(url.rsplit("/", 1)[-1]))[:120]
    dest = out_dir / name
    print(f"  ↓ downloading {name} …")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=1800) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    print(f"    saved {dest} ({dest.stat().st_size/1e6:.0f} MB)")
    return dest


def main():
    ap = argparse.ArgumentParser(description="Deep firmware finder by device/version.")
    ap.add_argument("query", help="device / model / version / codename terms (space = AND)")
    ap.add_argument("--android"); ap.add_argument("--region")
    ap.add_argument("--download", action="store_true", help="fetch DIRECT (no-login) hits")
    ap.add_argument("--out", default="~/Downloads/fw", help="download dir")
    ap.add_argument("--live", action="store_true", help="also query archive.org live")
    args = ap.parse_args()
    terms = [t.lower() for t in args.query.split()]

    corpus = search_corpus(terms, args.android, args.region)
    live = search_archive_org(args.query) if args.live else []

    # de-dupe live vs corpus by download_url
    seen = {r.get("download_url") for r in corpus}
    live = [r for r in live if r.get("download_url") not in seen]

    hits = corpus + live
    print(f"\n{len(hits)} match(es) for {args.query!r}"
          f"{' [+live archive.org]' if args.live else ''}\n")
    directs = []
    for h in sorted(hits, key=lambda x: (x.get("source", ""), x.get("device", ""))):
        gate, fetchable = GATE.get(h.get("source", ""), ("?", False))
        line = (f"[{gate:<7}] {h.get('source','?'):<16} {str(h.get('device',''))[:34]:<35} "
                f"{str(h.get('version') or h.get('model') or '')[:26]:<27} "
                f"{h.get('region') or ''} {h.get('android') or ''} {h.get('size') or ''}")
        print(line)
        if h.get("download_url"):
            print(f"          {h['download_url']}")
        if fetchable and h.get("download_url"):
            directs.append(h["download_url"])

    print(f"\n{len(directs)} directly-fetchable (no login). "
          f"{'Downloading…' if args.download else 'Re-run with --download to fetch them.'}")
    if args.download:
        for u in directs:
            try:
                download(u, args.out)
            except Exception as e:
                print(f"  download failed: {e}")


if __name__ == "__main__":
    main()
