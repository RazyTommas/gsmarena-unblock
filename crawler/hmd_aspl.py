#!/usr/bin/env python3
"""
hmd_aspl.py — Android patch levels for HMD / Nokia, from HMD's own security page.

    https://www.hmd.com/en_int/security-updates
    HTTP 200, ~910 KB, embedded JSON. robots.txt disallows /account, /cart and /api,
    none of which is this path.

    {"phone":"HMD Aura", "cadence":"Quarterly", "endOfLife":"Aug-26",
     "androidBulletinDate":"2026-06-01", "dateOfFirstLiveRelease":"July 06, 2026", ...}

THE TRAP, AND WHY THE DAY-OF-MONTH CHECK IS NOT OPTIONAL
The same JSON object carries TWO date fields and only one of them is a patch level:

    androidBulletinDate      2,872 values, day-of-month {01: 2872} — zero scatter
    dateOfFirstLiveRelease   "July 06, 2026" — a release date, scattered across the month

Picking the wrong key yields a full, plausible, entirely wrong patch column. This is
the third time that exact shape has appeared: mifirm.net's upload timestamps, and
sammobile's `CP` appearing only as prose in flashing instructions. So the check runs
before anything is written, and a distribution that is not concentrated on Google's
01/05 convention aborts the import rather than warning about it.

Found and verified by collector@field, who counted 102 distinct phones where a
subagent had reported 87 — the discrepancy is why the count here is recomputed
rather than taken on trust. This parser gets 84, which is a third number; the
distribution check is what makes the column trustworthy, not the count.

WORTH NOTHING TO THIS CORPUS, AND THAT IS THE FINDING.
84 phones parse cleanly and the distribution passes, but ZERO match our devices. We
hold 5 Nokia entries and every one is archival — 'Nokia BB5 Firmwares [Euro 3]',
'Nokia E6-00 Stock Firmware', 'Nokia Lumia 710 custom firmware collection'. There
are no modern HMD devices here for it to fill.

The module is kept because it is correct and cheap, and it works the day an HMD
device enters the corpus. But it is not coverage, and counting it as a "source
added" would be counting work rather than result. A verified source that matches
nothing is a negative, and recording it stops the next person re-deriving it.

    python3 hmd_aspl.py --dry-run
    python3 hmd_aspl.py
"""
from __future__ import annotations
import argparse, collections, json, re, sys, time
from common import connect as db_connect, http_get, log_run, host_budget

SRC = "https://www.hmd.com/en_int/security-updates"
HOST = "www.hmd.com"
REC_RE = re.compile(r'\{"phone":"(?P<phone>[^"]{2,60})".*?'
                    r'"androidBulletinDate":"(?P<spl>\d{4}-\d{2}-\d{2})"', re.S)


def fetch():
    ok, used, limit = host_budget(HOST)
    if not ok:
        raise SystemExit(f"daily budget for {HOST} exhausted ({used}/{limit})")
    html = http_get(SRC, timeout=60)
    if not html or "androidBulletinDate" not in html:
        raise SystemExit(f"{SRC} returned {len(html or '')}B with no androidBulletinDate "
                         f"— refusing, because an empty parse here is indistinguishable "
                         f"from 'HMD patched nothing'")
    return html


def parse(html):
    """{phone: newest patch level}. Non-greedy per record so a phone is never paired
    with a date belonging to the next object in the array."""
    out = {}
    for m in REC_RE.finditer(html):
        phone, spl = m.group("phone").strip(), m.group("spl")
        if phone and (phone not in out or spl > out[phone]):
            out[phone] = spl
    return out


def distribution_ok(values):
    """Google publishes patch levels on the 1st or the 5th. A column scattered across
    the month is a release date wearing this one's name."""
    dom = collections.Counter(v[-2:] for v in values)
    return set(dom) <= {"01", "05"}, dict(dom)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    phones = parse(fetch())
    if not phones:
        raise SystemExit("0 phones parsed from a page containing androidBulletinDate — "
                         "that is a parser failure, not an empty catalogue")
    ok, dom = distribution_ok(phones.values())
    print(f"  {len(phones)} distinct phones · patch levels "
          f"{min(phones.values())}..{max(phones.values())}")
    print(f"  day-of-month distribution: {dom}")
    if not ok:
        raise SystemExit("REFUSED: that distribution is not a patch-level column. "
                         "A scattered day-of-month means this is a release date, and "
                         "importing it would fill the column with confident wrong "
                         "values indistinguishable from measured ones.")
    print("  PASS — a real ASPL column")

    con = db_connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS hmd_aspl(
      phone TEXT PRIMARY KEY, spl TEXT, fetched_at TEXT);
    """)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def norm(s):
        s = re.sub(r"\s+", " ", (s or "").upper().strip())
        s = re.sub(r"[^A-Z0-9 ]", " ", s)
        s = re.sub(r"\b(HMD|NOKIA)\b", " ", s)      # brand prefix differs across sources
        return re.sub(r"\s+", " ", s).strip()

    corpus = {}
    for dev, in con.execute(
            "SELECT DISTINCT device FROM roms WHERE device LIKE 'Nokia%' "
            "OR device LIKE 'HMD%' OR vendor IN ('Nokia','HMD')"):
        corpus.setdefault(norm(dev), []).append(dev)

    matched, unmatched = [], []
    for phone, spl in phones.items():
        hit = corpus.get(norm(phone))
        if hit:
            matched.extend((d, spl) for d in hit)
        else:
            unmatched.append(phone)
    print(f"  {len(matched)} corpus devices matched · {len(unmatched)} HMD phones not in "
          f"our corpus")
    if a.dry_run:
        print("\n(dry run — nothing written)")
        for d, s in matched[:6]:
            print(f"    {d[:38]:40} {s}")
        if not matched:
            print(f"    sample HMD names : {sorted(phones)[:4]}")
            print(f"    sample our names : {sorted(sum(corpus.values(), []))[:4] or '(no Nokia/HMD devices in corpus)'}")
        return 0

    con.executemany("INSERT OR REPLACE INTO hmd_aspl VALUES(?,?,?)",
                    [(p, s, now) for p, s in phones.items()])
    con.executemany("UPDATE roms SET security_level=? "
                    "WHERE device=? AND IFNULL(security_level,'')=''", matched)
    con.commit()
    n = con.execute("SELECT COUNT(DISTINCT device) FROM roms "
                    "WHERE IFNULL(security_level,'')!=''").fetchone()[0]
    print(f"\n  devices with a patch level: {n:,}")
    log_run("hmd-aspl", len(matched), outcome="ok",
            note=f"{len(phones)} phones published, {len(matched)} matched to corpus")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
