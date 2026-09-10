#!/usr/bin/env python3
"""
fix_data.py — repair the defect classes audit.py finds. Verbose by design.

Every step prints what it is about to do, what it changed, and the before/after
number, so a long run is legible while it happens rather than after it fails.

    python fix_data.py --all          # everything (recommended)
    python fix_data.py --dedupe       # just one step
    python fix_data.py --all --dry-run

Nothing here invents data. Where a value is genuinely unknown it stays NULL and
is labelled unknown — a repair that fabricates is worse than the defect.
"""
from __future__ import annotations
import argparse, re, sqlite3, sys, time
from common import DB_PATH, log_run

T0 = time.time()


def log(msg, indent=0):
    print(f"[{time.time()-T0:6.1f}s] {'  '*indent}{msg}", flush=True)


def cols(con, table="roms"):
    return {d[1] for d in con.execute(f"PRAGMA table_info({table})")}


def addcol(con, name, typ="TEXT", table="roms"):
    if name not in cols(con, table):
        con.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {typ}')
        log(f"added column {table}.{name}", 1)


# ── 1. duplicates + a unique key so it can't recur ───────────────────────────
def dedupe(con, dry):
    log("STEP dedupe — collapse duplicate builds, then add a UNIQUE key")
    before = con.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    dups = con.execute("SELECT IFNULL(SUM(c-1),0) FROM (SELECT COUNT(*) c FROM roms "
                       "GROUP BY source,IFNULL(model,''),IFNULL(region,''),IFNULL(version,'') "
                       "HAVING c>1)").fetchone()[0]
    log(f"{before:,} rows, {dups:,} duplicates to remove", 1)
    if dry:
        return
    # keep the richest row per key: most non-null fields, then newest rowid
    con.execute("""
        DELETE FROM roms WHERE rowid NOT IN (
          SELECT rowid FROM (
            SELECT rowid,
                   ROW_NUMBER() OVER (
                     PARTITION BY source, IFNULL(model,''), IFNULL(region,''), IFNULL(version,'')
                     ORDER BY (CASE WHEN updated_at IS NOT NULL AND updated_at!='' THEN 1 ELSE 0 END)
                            + (CASE WHEN chipset IS NOT NULL AND chipset!='' THEN 1 ELSE 0 END)
                            + (CASE WHEN security_patch IS NOT NULL AND security_patch!='' THEN 1 ELSE 0 END)
                            DESC, rowid DESC) rn
            FROM roms) WHERE rn=1)""")
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    log(f"removed {before-after:,} rows -> {after:,}", 1)
    addcol(con, "build_key")
    con.execute("UPDATE roms SET build_key = source||'|'||IFNULL(model,'')||'|'||"
                "IFNULL(region,'')||'|'||IFNULL(version,'')")
    try:
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_roms_key ON roms(build_key)")
        log("UNIQUE index ux_roms_key created — re-runs can no longer duplicate", 1)
    except sqlite3.IntegrityError as e:
        log(f"could not add unique index ({e}) — residual dups remain", 1)
    con.commit()


# ── 2. relink firmware to devices on a normalised name ───────────────────────
def _norm(s):
    """Normalise a device name for matching.

    CAREFUL: the trailing-token strip exists for source codenames that FOLLOW a
    complete name ("Tecno Spark 30 KL6" -> "tecno spark 30"). It must never eat the
    model itself ("Samsung Galaxy A07" -> "samsung galaxy" would make every A-series
    device match the same spec row and get a confidently WRONG chipset). So it only
    fires when >=3 tokens survive.
    """
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = s.split("/")[0]
    s = re.sub(r"\b(5g|4g|lte|dual|sim)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    parts = s.split()
    # strip a trailing codename ONLY if the name still has >=3 real tokens after it
    if len(parts) >= 4 and re.fullmatch(r"[a-z]{1,2}\d[a-z0-9]{0,2}", parts[-1]):
        parts = parts[:-1]
    return " ".join(parts)


def relink(con, dry):
    log("STEP relink — attach firmware rows to devices by NORMALISED name")
    lut = {}
    for did, name in con.execute("SELECT device_id,name FROM devices"):
        k = _norm(name)
        if k:
            lut.setdefault(k, did)
    log(f"{len(lut):,} device keys", 1)
    before = con.execute("SELECT COUNT(*) FROM roms WHERE matched_devices IS NULL OR matched_devices=''").fetchone()[0]
    log(f"{before:,} unlinked rows", 1)
    if dry:
        return
    n = 0
    rows = con.execute("SELECT rowid,device FROM roms WHERE matched_devices IS NULL OR matched_devices=''").fetchall()
    for i, (rid, dev) in enumerate(rows, 1):
        k = _norm(dev); did = lut.get(k)
        if not did:
            parts = k.split()
            while len(parts) > 3 and not did:
                parts = parts[:-1]; did = lut.get(" ".join(parts))
        if did:
            con.execute("UPDATE roms SET matched_devices=? WHERE rowid=?", (did, rid)); n += 1
        if i % 10000 == 0:
            con.commit(); log(f"scanned {i:,}/{len(rows):,} · linked {n:,}", 2)
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM roms WHERE matched_devices IS NULL OR matched_devices=''").fetchone()[0]
    log(f"linked {n:,} rows; still unlinked {after:,} (no matching device sheet)", 1)


# ── 3. region vocabulary ─────────────────────────────────────────────────────
def normalize_regions(con, dry):
    log("STEP normalize-regions — keep CSC codes and country names apart")
    addcol(con, "region_kind")
    bad = [r[0] for r in con.execute("SELECT DISTINCT region FROM roms WHERE region!='' "
                                     "AND region NOT GLOB '[A-Z][A-Z][A-Z]'")]
    log(f"{len(bad)} non-CSC region values: {', '.join(bad[:8])}", 1)
    if dry:
        return
    con.execute("UPDATE roms SET region_kind = CASE "
                "WHEN region IS NULL OR region='' THEN 'none' "
                "WHEN region GLOB '[A-Z][A-Z][A-Z]' THEN 'csc' ELSE 'market' END")
    con.commit()
    for k, n in con.execute("SELECT region_kind,COUNT(*) FROM roms GROUP BY region_kind"):
        log(f"region_kind={k}: {n:,}", 2)


# ── 4. security column carried two meanings ─────────────────────────────────
def split_security(con, dry):
    log("STEP split-security — one column held BOTH advisory URLs and patch dates")
    addcol(con, "security_url"); addcol(con, "security_level")
    u = con.execute("SELECT COUNT(*) FROM roms WHERE security_patch LIKE 'http%'").fetchone()[0]
    d = con.execute("SELECT COUNT(*) FROM roms WHERE security_patch GLOB '[0-9][0-9][0-9][0-9]-*'").fetchone()[0]
    log(f"{u:,} URLs, {d:,} patch levels", 1)
    if dry:
        return
    con.execute("UPDATE roms SET security_url=security_patch WHERE security_patch LIKE 'http%'")
    con.execute("UPDATE roms SET security_level=security_patch "
                "WHERE security_patch GLOB '[0-9][0-9][0-9][0-9]-*'")
    con.commit()
    log("split done — UI can render a link as a link and a date as a date", 1)


# ── 5. links that don't deliver what the label promises ─────────────────────
def mark_links(con, dry):
    log("STEP mark-links — label a page link as a page, not a download")
    addcol(con, "link_kind")
    n = con.execute("SELECT COUNT(*) FROM roms WHERE download_url IS NOT NULL "
                    "AND download_url=model_url").fetchone()[0]
    log(f"{n:,} rows whose 'download' is actually the model page", 1)
    if dry:
        return
    con.execute("UPDATE roms SET link_kind = CASE "
                "WHEN download_url IS NULL OR download_url='' THEN 'none' "
                "WHEN download_url=model_url THEN 'page' "
                "WHEN download_url LIKE '%.zip' OR download_url LIKE '%.rar' "
                "  OR download_url LIKE '%.ipsw' OR download_url LIKE '%.7z' "
                "  OR download_url LIKE '%.tar%' THEN 'file' ELSE 'redirect' END")
    con.commit()
    for k, c in con.execute("SELECT link_kind,COUNT(*) FROM roms GROUP BY link_kind ORDER BY 2 DESC"):
        log(f"link_kind={k}: {c:,}", 2)


# ── 6. junk names ────────────────────────────────────────────────────────────
def clean_names(con, dry):
    log("STEP clean-names — truncation markers and doubled vendor prefixes")
    t = con.execute("SELECT COUNT(*) FROM roms WHERE device LIKE '%...'").fetchone()[0]
    log(f"{t:,} truncated names", 1)
    if dry:
        return
    addcol(con, "name_truncated", "INTEGER")
    con.execute("UPDATE roms SET name_truncated = CASE WHEN device LIKE '%...' THEN 1 ELSE 0 END")
    # strip the marker but flag it, so the name is usable AND its provenance is honest
    con.execute("UPDATE roms SET device=TRIM(REPLACE(device,'...','')) WHERE device LIKE '%...'")
    for v in ("Samsung", "Tecno", "Xiaomi"):
        con.execute("UPDATE roms SET device=? || SUBSTR(device, LENGTH(?)*2+2) "
                    "WHERE device LIKE ? || ' ' || ? || ' %'", (v, v, v, v))
    con.commit()
    log("names cleaned; name_truncated flags the ones that were cut off at source", 1)


# ── 7. OS values that break numeric comparison ──────────────────────────────
def clean_os(con, dry):
    log("STEP clean-os — non-numeric OS values vanish from 'Android >= n'")
    bad = [r[0] for r in con.execute("SELECT DISTINCT android FROM roms WHERE android!='' "
                                     "AND CAST(android AS REAL)=0")]
    log(f"non-numeric OS values: {bad}", 1)
    if dry:
        return
    addcol(con, "android_num", "REAL")
    con.execute("UPDATE roms SET android_num = CASE WHEN android IS NULL OR android='' THEN NULL "
                "WHEN CAST(android AS REAL)>0 THEN CAST(android AS REAL) ELSE NULL END")
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM roms WHERE android_num IS NOT NULL").fetchone()[0]
    log(f"android_num set on {n:,} rows; unparseable ones are NULL (never silently 0)", 1)


def clamp_dates(con, dry):
    log("STEP clamp-dates — future-dated rows")
    n = con.execute("SELECT COUNT(*) FROM roms WHERE updated_at>date('now')").fetchone()[0]
    log(f"{n:,} future dates", 1)
    if n and not dry:
        addcol(con, "date_suspect", "INTEGER")
        con.execute("UPDATE roms SET date_suspect=1 WHERE updated_at>date('now')")
        con.commit(); log("flagged (not rewritten — the source said so)", 1)


STEPS = [("dedupe", dedupe), ("relink", relink), ("normalize-regions", normalize_regions),
         ("split-security", split_security), ("mark-links", mark_links),
         ("clean-names", clean_names), ("clean-os", clean_os), ("clamp-dates", clamp_dates)]


def main():
    ap = argparse.ArgumentParser(description="Repair corpus defects (verbose).")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    for name, _ in STEPS:
        ap.add_argument("--" + name, action="store_true")
    a = ap.parse_args()
    chosen = [(n, f) for n, f in STEPS if a.all or getattr(a, n.replace("-", "_"))]
    if not chosen:
        ap.error("pick --all or at least one step")
    con = sqlite3.connect(DB_PATH)
    log(f"corpus: {con.execute('SELECT COUNT(*) FROM roms').fetchone()[0]:,} rows"
        f"{'  [DRY RUN]' if a.dry_run else ''}")
    for name, fn in chosen:
        try:
            fn(con, a.dry_run)
        except Exception as e:
            log(f"STEP {name} FAILED: {type(e).__name__}: {e}", 1)
    n = con.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    con.close()
    log(f"done — {n:,} rows")
    if not a.dry_run:
        log_run("fix-data", n)


if __name__ == "__main__":
    main()
