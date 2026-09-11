#!/usr/bin/env python3
"""
chipset_map.py — the PART NUMBER <-> MARKETING NAME bridge, and part classification.

WHY THIS FILE EXISTS (measured, not assumed):
  * Chipset advisories name chips by PART NUMBER only. MediaTek bulletins are
    part-number-exclusive; Qualcomm bulletins carry both but NEVER in the same row
    (measured overlap: 0 across three bulletins) — so a bulletin is a CONSUMER of a
    crosswalk, never a source of one.
  * Our device corpus is the mirror image: 95% of Qualcomm chipset strings carry a
    part number, but 0 of 638 Dimensity strings do. Older Helio strings do (79%).
  * Therefore: for Qualcomm the bridge is an optimisation. For MediaTek Dimensity it
    is the ONLY path from an advisory to a device. Without it that whole vendor is
    unjoinable and the UI would silently under-report.

SOURCES, and why each is trusted only so far:
  wiki      Wikipedia SoC list, rowspan-aware parse. Best coverage (74% of real
            advisory parts) but the tables misalign at the tail, which produced
            MT6582 -> 'Helio G99+' (MT6582 is a 2013 Cortex-A7; the Helio brand did
            not exist until 2015). Era-checked below.
  cpufetch  cpufetch's SoC table. Cleaner, thinner (57%).
  gsm       mined from our own device corpus. Contains scrape junk
            ('MT6582' -> '- India model'), so every value is shape-validated.

CONFIDENCE is earned, never assumed:
  verified   >= 2 independent sources agree
  tentative  exactly 1 source, value passes shape + era checks
  rejected   fails a check, or sources disagree  (stored, so it is auditable,
             but never used for a join)

PART CLASSIFICATION matters as much as the bridge. One Qualcomm advisory named 476
parts and only 16% were application processors — 78% were PMIC/RF/Wi-Fi/codec. Of
212 MediaTek parts across 21 bulletins only 38% were the smartphone band; the rest
were TVs, Chromebooks and Wi-Fi. Joining without classifying first invents coverage
that is not there.
"""
from __future__ import annotations
import json, re, sqlite3, sys
from pathlib import Path
from common import DB_PATH

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

# ── what a real marketing name looks like (rejects scrape junk) ──────────────
MKT_SHAPE = {
    "mediatek": re.compile(r"^(Dimensity\s+\d{3,4}[\w+]*(?:\s+(?:Ultra|Ultimate|Pro))?"
                           r"|Helio\s+[APGX]\d{1,3}[\w+]*)$", re.I),
    "qualcomm": re.compile(r"^Snapdragon\s+[\w\s\.\+]{1,28}$", re.I),
    "samsung":  re.compile(r"^Exynos\s+[\w\d\s]{1,20}$", re.I),
}

# ── era constraint: a brand cannot predate itself ───────────────────────────
# MediaTek's Helio brand launched 2015 and first appeared on MT6795; the Helio G
# line on MT6769-class parts (2019); Dimensity on MT6853-class (2020). A part
# numbered below that band carrying such a name is a table misalignment, not a chip.
MT_MIN_FOR_BRAND = [
    (re.compile(r"^Dimensity", re.I), 6700),
    (re.compile(r"^Helio\s+G", re.I), 6700),
    (re.compile(r"^Helio\s+[APX]", re.I), 6580),
]

# ── part families: only an application processor runs the firmware we track ──
# Anything else in an advisory (PMIC, RF front-end, Wi-Fi, codec, modem) ships
# inside the phone but is NOT what a Security Patch Level speaks about.
PART_CLASS = [
    ("ap",        re.compile(r"^(SM\d{4}|SDM\d{3,4}|MSM\d{4}|APQ\d{4}|QSD\d{4}"
                             r"|MT6[5-9]\d{2}|S5E\d{4}|S5P\d{4})", re.I)),
    ("iot",       re.compile(r"^(QCS\d{3,4}|QCM\d{3,4}|MT8[13]\d{2})", re.I)),
    ("auto",      re.compile(r"^(SA\d{4}|QAM\d{4})", re.I)),
    ("modem",     re.compile(r"^(MDM\d{4}|SDX\d{2,3}|X\d{2}\b)", re.I)),
    ("wifi",      re.compile(r"^(QCA\d{4}|WCN\d{4}|MT7\d{3}|MT79\d{2})", re.I)),
    ("audio",     re.compile(r"^(WCD\d{4}|WSA\d{4}|AQT\d{4}|MT66\d{2})", re.I)),
    ("power",     re.compile(r"^(PM\w?\d{3,4}|SMB\d{4}|MT63\d{2})", re.I)),
    ("rf",        re.compile(r"^(QET\d{4}|QPM\d{4}|QDM\d{4}|QFE\d{4}|QTC\d{4}"
                             r"|SDR\d{3,4}|WTR\d{4})", re.I)),
    ("tv",        re.compile(r"^(MT5\d{3}|MT9\d{3})", re.I)),
    ("chromebook", re.compile(r"^MT81\d{2}", re.I)),
]


def classify_part(part: str) -> str:
    """Which silicon family a part number belongs to. 'ap' is the only class whose
    vulnerabilities a firmware Security Patch Level can be said to address."""
    p = (part or "").upper().strip()
    for name, pat in PART_CLASS:
        if pat.match(p):
            return name
    return "unknown"


def _mt_number(part: str):
    m = re.match(r"^MT(\d{4})", (part or "").upper())
    return int(m.group(1)) if m else None


def _era_ok(vendor: str, part: str, mkt: str) -> bool:
    if vendor != "mediatek":
        return True
    n = _mt_number(part)
    if n is None:
        return True
    for pat, floor in MT_MIN_FOR_BRAND:
        if pat.match(mkt):
            return n >= floor
    return True


def _vendor_of(part: str) -> str | None:
    p = (part or "").upper()
    if p.startswith("MT"):
        return "mediatek"
    if re.match(r"^(SM|SDM|MSM|APQ|QSD|QCS|QCM|SA|MDM|SDX|QCA|WCN|WCD|WSA|AQT|PM|QET|QPM|QDM|QFE|SDR|WTR)", p):
        return "qualcomm"
    if p.startswith("S5"):
        return "samsung"
    return None


def canon(mkt: str) -> str:
    """Comparison form: lowercase, radio-generation suffix dropped, spacing collapsed.
    'Snapdragon 680 4G' and 'Snapdragon 680' are the same silicon."""
    s = (mkt or "").lower()
    s = re.sub(r"\s*\b[45]g\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _clean_mkt(mkt: str) -> str:
    """Scraped names carry a commentary tail glued on by the table parse
    ('Snapdragon 600(Advertised as S4 Pro)'). The brand+number prefix is good data;
    only the tail is junk, so trim rather than discard the whole claim."""
    s = re.sub(r"\s+", " ", (mkt or "")).strip()
    s = re.split(r"\s*[\(\[,;/]|\s+\bformerly\b|\s+\baka\b", s, 1)[0].strip()
    return re.sub(r"[\s\-–—:]+$", "", s)


def load_sources() -> dict:
    """part -> {source: marketing}. Values are shape+era checked on the way in."""
    claims: dict[str, dict[str, str]] = {}
    rejects: list[tuple] = []

    def offer(part, mkt, src):
        part = (part or "").upper().strip()
        mkt = _clean_mkt(mkt)
        if not part or not mkt or part == mkt:
            return
        vend = _vendor_of(part)
        if not vend:
            return
        shape = MKT_SHAPE.get(vend)
        if shape and not shape.match(mkt):
            rejects.append((part, mkt, src, "shape"))
            return
        if not _era_ok(vend, part, mkt):
            rejects.append((part, mkt, src, "era"))
            return
        claims.setdefault(part, {})[src] = mkt

    for fn, src in (("mt_wiki_xref.json", "wiki"), ("qc_wiki_xref.json", "wiki")):
        p = DATA / fn
        if p.exists():
            for k, v in json.loads(p.read_text()).items():
                offer(k, v, src)
    p = DATA / "gsm_xref.json"
    if p.exists():
        for k, v in json.loads(p.read_text()).items():
            offer(k, v, "gsm")
    p = DATA / "cpufetch_map.json"
    if p.exists():
        for k, v in json.loads(p.read_text()).items():
            # cpufetch values are [name, vendor]; a name equal to the part is a self-ref
            if isinstance(v, list) and v:
                offer(k, v[0], "cpufetch")

    # Our own corpus is the STRONGEST source for the pairs it has: a spec string like
    # "Qualcomm SM8750-AC Snapdragon 8 Elite (3 nm)" carries the part number and the
    # marketing name in ONE string from ONE vendor-published spec sheet, so the
    # correlation cannot have been lost in transit. (Advisories never do this —
    # measured co-occurrence there is 0.) This is also why the bridge stays current
    # without us curating flagship parts by hand.
    try:
        import chipset_ids
        con = sqlite3.connect(DB_PATH)
        seen = set()
        for q in ('SELECT DISTINCT chipset FROM roms WHERE IFNULL(chipset,\'\')!=\'\'',
                  'SELECT DISTINCT "Platform — Chipset" FROM devices '
                  'WHERE IFNULL("Platform — Chipset",\'\')!=\'\''):
            try:
                rows = [r[0] for r in con.execute(q)]
            except sqlite3.OperationalError:
                continue
            for s in rows:
                if s in seen:
                    continue
                seen.add(s)
                pr = chipset_ids.parse(s)
                if pr and pr.get("part") and pr.get("marketing"):
                    offer(pr["part"], pr["marketing"], "corpus")
        con.close()
    except Exception as e:
        print(f"  (corpus mining skipped: {type(e).__name__}: {e})")
    return claims, rejects


def build(con=None, verbose=True):
    own = con is None
    con = con or sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS chipset_map(
      part TEXT PRIMARY KEY, vendor TEXT, marketing TEXT, part_class TEXT,
      confidence TEXT, sources TEXT, note TEXT);
    CREATE INDEX IF NOT EXISTS ix_cmap_mkt ON chipset_map(LOWER(marketing));
    CREATE TABLE IF NOT EXISTS chipset_map_reject(
      part TEXT, marketing TEXT, source TEXT, reason TEXT);
    """)
    con.execute("DELETE FROM chipset_map")
    con.execute("DELETE FROM chipset_map_reject")

    claims, rejects = load_sources()
    counts = {"verified": 0, "tentative": 0, "disputed": 0}
    for part, by_src in claims.items():
        vals = {}
        for src, mkt in by_src.items():
            vals.setdefault(canon(mkt), []).append(src)
        # Agreement is judged on the CANONICAL name. Sources differ cosmetically on a
        # radio suffix ('Snapdragon 680' vs 'Snapdragon 680 4G') and calling that a
        # conflict buried the handful of real ones (MSM8996: 820 vs 821; MT6833V: 700
        # vs 810) under 30 items of noise. Canonicalise, then a dispute means a dispute.
        best = max(vals.items(), key=lambda kv: len(kv[1]))
        # 2-of-3 agreement outranks a lone dissenter — that is what independent
        # sources are FOR. Only a 1-vs-1 split is genuinely unresolvable.
        if len(vals) > 1 and len(best[1]) >= 2:
            conf, note = "verified", ("majority over " + "/".join(
                k for k in vals if k != best[0]))
        elif len(vals) > 1:
            conf, note = "disputed", " | ".join(f"{k}({','.join(v)})" for k, v in vals.items())
        elif len(best[1]) >= 2:
            conf, note = "verified", ""
        else:
            conf, note = "tentative", "single source"
        counts[conf] = counts.get(conf, 0) + 1
        mkt = max((m for m in by_src.values() if canon(m) == best[0]), key=len)
        con.execute("INSERT OR REPLACE INTO chipset_map VALUES(?,?,?,?,?,?,?)",
                    (part, _vendor_of(part), mkt, classify_part(part), conf,
                     ",".join(sorted(best[1])), note))
    con.executemany("INSERT INTO chipset_map_reject VALUES(?,?,?,?)", rejects)
    con.commit()

    if verbose:
        print(f"chipset_map: {sum(counts.values()):,} part->name bridges "
              f"({counts['verified']} verified · {counts['tentative']} tentative · "
              f"{counts.get('disputed',0)} disputed)")
        print(f"  {len(rejects)} claims rejected before storage:")
        for reason in ("shape", "era"):
            rs = [r for r in rejects if r[3] == reason]
            if rs:
                print(f"    {reason}: {len(rs)}  e.g. {rs[0][0]} -> {rs[0][1][:34]!r}")
        for vend in ("mediatek", "qualcomm", "samsung"):
            n = con.execute("SELECT COUNT(*) FROM chipset_map WHERE vendor=? "
                            "AND confidence!='disputed'", (vend,)).fetchone()[0]
            ap = con.execute("SELECT COUNT(*) FROM chipset_map WHERE vendor=? "
                             "AND part_class='ap' AND confidence!='disputed'", (vend,)).fetchone()[0]
            print(f"  {vend:9} {n:4} usable  ({ap} application processors)")
    if own:
        con.close()
    return counts


def part_for_marketing(con, name: str):
    """Marketing name -> part numbers. The MediaTek direction: a Dimensity spec
    string has no part number, so this is how its advisories are reached."""
    if not name:
        return []
    return [r[0] for r in con.execute(
        "SELECT part FROM chipset_map WHERE LOWER(marketing)=? AND confidence!='disputed'",
        (name.strip().lower(),))]


def marketing_for_part(con, part: str):
    r = con.execute("SELECT marketing FROM chipset_map WHERE part=? AND confidence!='disputed'",
                    ((part or "").upper(),)).fetchone()
    return r[0] if r else None


if __name__ == "__main__":
    build()
    con = sqlite3.connect(DB_PATH)
    print("\nspot-check (independently known pairs):")
    for p in ("MT6991", "MT6897", "MT6835", "MT6789", "MT6582", "SM8650", "SM8750"):
        m = marketing_for_part(con, p)
        print(f"  {p:8} -> {m or '(none — not bridged)'}")
    print("\nreverse, the MediaTek-critical direction:")
    for n in ("Dimensity 9400", "Dimensity 6100+", "Helio G99", "Helio G85"):
        print(f"  {n:16} -> {part_for_marketing(con, n) or '(none)'}")
    con.close()
