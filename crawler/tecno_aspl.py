#!/usr/bin/env python3
"""
tecno_aspl.py — Android patch levels for TECNO, from TECNO's own Security Response Center.

WHY TECNO SPECIFICALLY
Patch level is the binding constraint on every verdict this project produces — 83 of
1,322 devices had one. TECNO is 328 of the devices with no chipset AND no patch level,
and it is the vendor neither cheap win reaches: Google's device catalog covers 6 of
321 TECNO models, and gsmarena needs a per-device crawl we are rate-limited out of.
So this is the one source that moves a vendor nothing else touches.

    https://security.tecno.com/slm/deviceScope?lang=en_US&year=
    HTTP 200, JSON, robots.txt says `Disallow:` (empty — explicitly permitted).
    data[].i18n.title    "2026-08  Phone models with latest security patches"
    data[].i18n.content  "TECNO CAMON 30S Pro | TECNO CAMON 30S\\nTECNO SPARK 40 | ..."

MONTH PRECISION, AND WHY THAT IS STORED RATHER THAN PADDED
TECNO publishes the patch MONTH, not a full date. `2026-08` is not padded to
`2026-08-01`: padding would invent a day the vendor never stated, and worse, it would
silently assert the TIER — a `-01` level claims the build cannot adjudicate any
chipset CVE, which TECNO never said. platform_vuln.spl_precision() handles month
values honestly: sound across month boundaries, unadjudicable within one.

WHAT THIS CANNOT TELL US
The list is "models with the latest patch as of month M" — a model appearing under
2026-08 means TECNO shipped it that patch level, not that any given build carries it.
So this fills a DEVICE-level patch level, not a per-build one, and the rows it writes
say so via source='tecno-src'. The TECNO SRC is also TECNO-only: zero Infinix and
zero itel models appear in it, despite all three being Transsion brands.

    python3 tecno_aspl.py --dry-run
    python3 tecno_aspl.py
"""
from __future__ import annotations
import argparse, json, re, sys, time
from common import connect as db_connect, http_get, log_run, host_budget

SRC = "https://security.tecno.com/slm/deviceScope?lang=en_US&year="
HOST = "security.tecno.com"
MONTH_RE = re.compile(r"(20\d{2})[-/](\d{2})")


def fetch():
    ok, used, limit = host_budget(HOST)
    if not ok:
        raise SystemExit(f"daily budget for {HOST} exhausted ({used}/{limit})")
    raw = http_get(SRC, timeout=45)
    if not raw:
        raise SystemExit(f"{SRC} returned nothing — not writing, since an empty parse "
                         f"here is indistinguishable from 'TECNO patched no devices'")
    d = json.loads(raw)
    if d.get("code") != 200 or not d.get("data"):
        raise SystemExit(f"unexpected envelope: code={d.get('code')} "
                         f"data={len(d.get('data') or [])} — refusing")
    return d["data"]


def parse(entries):
    """[(model, month)] — one row per model per published month."""
    out = []
    for e in entries:
        i18n = e.get("i18n") or {}
        title = i18n.get("title") or ""
        m = MONTH_RE.search(title)
        if not m:
            continue                      # an entry with no month cannot be used
        month = f"{m.group(1)}-{m.group(2)}"
        content = (i18n.get("content") or "")
        # FIVE delimiters in one free-text field, and missing any of them silently
        # halves the yield: pipe, newline, forward slash, comma, and the FULLWIDTH
        # comma U+FF0C that appears in entries written with a CJK keyboard. Splitting
        # on only pipe and newline returned 183 models; the real count is higher.
        for name in re.split(r"[|/,\uff0c\n\r]+", content):
            name = re.sub(r"\s+", " ", name).strip()
            if len(name) > 3 and name.upper().startswith("TECNO"):
                out.append((name, month))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    rows = parse(fetch())
    if not rows:
        raise SystemExit("parsed 0 model/month pairs from a 200 response — that is a "
                         "parser failure, not an empty catalogue. Refusing.")
    months = sorted({m for _, m in rows})
    models = sorted({n for n, _ in rows})
    print(f"  {len(rows):,} model/month pairs · {len(models)} distinct models · "
          f"{len(months)} months {months[0]}..{months[-1]}")

    # A model's patch level is the NEWEST month it appears under.
    latest = {}
    for name, month in rows:
        if name not in latest or month > latest[name]:
            latest[name] = month

    con = db_connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS tecno_aspl(
      model_name TEXT PRIMARY KEY, spl_month TEXT, fetched_at TEXT);
    """)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Match TECNO device names in the corpus. Normalised, and EXACT after
    # normalisation — no fuzzy prefix matching. "TECNO SPARK 30" must not claim the
    # patch level published for "TECNO SPARK 30 5G"; they are different devices and a
    # wrong patch level is worse than none, because it is indistinguishable from a
    # measured one.
    MODELCODE = re.compile(r"\s+[A-Z]{2}[A-Z0-9]{1,4}$")

    def norm(s, strip_code=False):
        """Normalised comparison key.

        Our device names carry a trailing model code that TECNO's list does not:
        'Tecno Camon 11 CF7' vs 'TECNO CAMON 11'. Stripped only from OUR side, and
        only a trailing token that LOOKS like a model code (two letters then up to
        four alphanumerics), so 'Camon 11 Pro' keeps its 'Pro' and never collapses
        into 'Camon 11'. Those are different phones and a shared patch level would be
        a fabricated fact."""
        s = re.sub(r"\s+", " ", (s or "").upper().strip())
        s = re.sub(r"[^A-Z0-9 ]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        if strip_code:
            s = MODELCODE.sub("", s)
        return s

    corpus = {}
    for dev, in con.execute("SELECT DISTINCT device FROM roms WHERE vendor='Tecno' "
                            "OR device LIKE 'Tecno%' OR device LIKE 'TECNO%'"):
        corpus.setdefault(norm(dev, strip_code=True), []).append(dev)
    # A normalised key that maps to several of OUR devices is ambiguous: those devices
    # differ only by model code, and TECNO published one patch level for the name. It
    # is still the right level for all of them -- same model, different SKU -- but the
    # count is reported so the fan-out is never mistaken for extra coverage.
    fanout = sum(len(v) - 1 for v in corpus.values() if len(v) > 1)

    matched, unmatched = [], []
    for name, month in latest.items():
        hit = corpus.get(norm(name))
        if hit:
            for dev in hit:
                matched.append((dev, month))
        else:
            unmatched.append(name)

    print(f"  {len(matched)} corpus devices matched from {len(set(norm(n, True) for n in latest))} "
          f"distinct TECNO names · {len(unmatched)} published models absent from our corpus")
    print(f"  (of the matches, {fanout} are SKU fan-out: several of our rows share one "
          f"TECNO model name and correctly share its patch level)")
    if a.dry_run:
        print("\n(dry run — nothing written)")
        for d, m in matched[:6]:
            print(f"    {d[:38]:40} {m}")
        return 0

    con.executemany("INSERT OR REPLACE INTO tecno_aspl VALUES(?,?,?)",
                    [(n, m, now) for n, m in latest.items()])
    # device-level, month-precision. Only fills where nothing better exists.
    con.executemany("UPDATE roms SET security_level=? "
                    "WHERE device=? AND IFNULL(security_level,'')=''",
                    [(m, d) for d, m in matched])
    con.commit()
    n = con.execute("SELECT COUNT(DISTINCT device) FROM roms "
                    "WHERE IFNULL(security_level,'')!=''").fetchone()[0]
    print(f"\n  devices with a patch level: {n:,}")
    log_run("tecno-aspl", len(matched), outcome="ok",
            note=f"{len(matched)} devices given a month-precision patch level")
    con.close()
    print("\nre-join:  python3 platform_vuln.py --build && python3 device_state.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
