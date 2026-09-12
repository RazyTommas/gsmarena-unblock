#!/usr/bin/env python3
"""
platform_vuln.py — Android PLATFORM exposure, which needs no chipset at all.

WHY THIS IS A SEPARATE LANE FROM vuln.py
The chipset lane is gated on knowing a device's silicon, and that is our scarcest
column. The platform lane is not: an AOSP/Framework/Kernel CVE applies to a device
because it runs Android, and the Security Patch Level alone adjudicates it. So this
lane reaches every build that carries an SPL, whether or not we know its chipset.

WHAT WAS BEING THROWN AWAY
osv_spl.py already ingests the whole Android bulletin — 1,574 tier-01 CVEs and 2,075
tier-05 — and vuln.py used exactly none of the tier-01 set (its filter is
`WHERE spl_tier=5`, correct for chipsets, wrong as a corpus-wide rule). Two measured
examples of what that cost, both flagged by Google as exploited in the wild and both
reaching ZERO devices before this module existed:

  CVE-2025-48543  tier-01, SPL 2025-09-01 — discarded by the tier filter.
  CVE-2025-38352  tier-05, SPL 2025-09-05 — a Linux *kernel* CVE, so it never appears
                  in chipset_cve and the chipset lane cannot see it at all.

They fail for different reasons, which is why the fix is a lane and not a one-line
filter change.

THE TIER RULE, STATED GENERALLY
Android defines two cumulative patch levels per month. `-01` carries AOSP platform
issues (Framework, System, Runtime, Setup Wizard, Mainline); `-05` carries those PLUS
Kernel, Arm, and every silicon-vendor section. So:

    a device can adjudicate a CVE only if  device_tier >= cve_tier

A `-05` build covers both tiers. A `-01` build covers tier-01 CVEs — and genuinely
cannot speak to a tier-05 one. This subsumes the chipset lane's `-01` guard rather
than contradicting it: chipset CVEs are all tier-05, so `device_tier >= 5` fails for
every `-01` build, exactly as before.

VOLUME CONTROL IS PART OF CORRECTNESS
Joining every device to every CVE in the bulletin's history produces ~398,000 rows,
overwhelmingly "your 2026 phone is fixed against a 2020 bug". That is not a finding,
it is a denominator that drowns the findings. Two predicates keep it honest:
  * the CVE must not predate the device's own firmware history by more than
    RELEVANCE_MONTHS (a fix shipped before the device existed is not news), and
  * open findings are always reported with their denominator, never alone.
Both are recorded on the row, so a reader can widen them and see what changes.

    python3 platform_vuln.py --build
    python3 platform_vuln.py --device "Samsung Galaxy A07"
"""
from __future__ import annotations
import argparse, re, sqlite3
from common import DB_PATH, log_run

# How far back a patch level is still worth adjudicating for a given build. 24 months
# covers the realistic support window of the devices we track while cutting the
# "fixed against a bug from before this phone shipped" noise.
RELEVANCE_MONTHS = 24


def tier_of(spl: str):
    """1 or 5 from a FULL patch-level date; None when it carries no tier (never guess).

    The length check is load-bearing: '2026-01' is a patch MONTH and ends in '-01', so
    a bare endswith() read January as tier-1 and May as tier-5, inventing a tier from a
    value that has none."""
    s = (spl or "").strip()
    if len(s) != 10:
        return None
    if s.endswith("-01"):
        return 1
    if s.endswith("-05"):
        return 5
    return None


def spl_precision(spl: str):
    """'date' (YYYY-MM-DD) or 'month' (YYYY-MM), or None if it is neither.

    Six of the nine non-Samsung vendors that publish a patch level publish only the
    MONTH. Three ways to model that and only one is honest:

      * demand a full date  -> every one of them fails, several hundred devices lost
      * pad '2026-08' to '2026-08-01' -> invents a day the vendor never stated, and
        from then on it is indistinguishable from a real -01 level. That is worse
        than losing the data, because it silently asserts the TIER as well: a padded
        -01 would claim the build cannot adjudicate chipset CVEs, which the vendor
        never said.
      * store the precision and let the comparison respect it. <- this

    A month-precision level still adjudicates plenty: if a fix shipped at 2026-07-05
    and the device reports 2026-08, August is later than July whichever day it means.
    It is only ambiguous WITHIN the same month, where -01 and -05 differ. So month
    precision is sound across month boundaries and unadjudicable inside one."""
    s = (spl or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return "date"
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return "month"
    return None


def _months_between(a: str, b: str) -> int:
    """Whole months from a to b, both 'YYYY-MM…'. Negative when a is later."""
    try:
        ay, am = int(a[:4]), int(a[5:7])
        by, bm = int(b[:4]), int(b[5:7])
    except (ValueError, IndexError):
        return 0
    return (by - ay) * 12 + (bm - am)


def build(con=None, verbose=True, relevance_months=RELEVANCE_MONTHS):
    own = con is None
    con = con or sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS platform_vuln(
      device TEXT, vendor TEXT, region TEXT, version TEXT, android TEXT,
      spl TEXT, spl_tier INTEGER, cve TEXT, cve_tier INTEGER, fix_spl TEXT,
      severity TEXT, osv_id TEXT, vendor_ref TEXT, asb_url TEXT,
      status TEXT, in_window INTEGER,
      PRIMARY KEY (device, region, version, cve, fix_spl));
    CREATE INDEX IF NOT EXISTS ix_pv_dev ON platform_vuln(device);
    CREATE INDEX IF NOT EXISTS ix_pv_status ON platform_vuln(status);
    CREATE INDEX IF NOT EXISTS ix_pv_cve ON platform_vuln(cve);
    CREATE TABLE IF NOT EXISTS platform_coverage(
      device TEXT, region TEXT, version TEXT, android TEXT, spl TEXT, spl_tier INTEGER,
      open_n INTEGER, fixed_n INTEGER, unadjudicable_n INTEGER, applicable_n INTEGER,
      window_months INTEGER, skipped_out_of_window INTEGER,
      PRIMARY KEY (device, region, version));
    """)
    con.execute("DELETE FROM platform_vuln")
    con.execute("DELETE FROM platform_coverage")

    # The bulletin, at its own grain: one row per (cve, patch level). A CVE can be
    # re-listed at more than one level; the EARLIEST is what a build must reach.
    cves = {}
    for cve, spl, tier, osv, sev, vref, asb in con.execute(
            "SELECT cve, spl, spl_tier, osv_id, severity, vendor_ref, asb_url FROM cve_spl"):
        t = tier if tier in (1, 5) else tier_of(spl)
        if t is None:
            continue          # a level we cannot tier is a level we cannot adjudicate
        prev = cves.get(cve)
        if prev is None or spl < prev[0]:
            cves[cve] = (spl, t, osv, sev, vref, asb)

    # Latest build per (device, region) that carries a patch level. No chipset needed.
    rows = list(con.execute("""
        SELECT device, vendor, region, version, android, security_level, updated_at
        FROM roms WHERE IFNULL(security_level,'')!=''
        ORDER BY device, region, IFNULL(updated_at,'') DESC, version DESC"""))
    latest, seen = [], set()
    for r in rows:
        k = (r[0], r[2])
        if k not in seen:
            seen.add(k); latest.append(r)

    # Each device's own floor: the oldest patch level we have ever seen it on. A CVE
    # fixed long before that is not something this device was ever exposed to in our
    # record, and saying "fixed" about it is filler, not information.
    floor = {}
    for dev, spl in con.execute("""SELECT device, MIN(security_level) FROM roms
                                   WHERE IFNULL(security_level,'')!='' GROUP BY device"""):
        floor[dev] = spl

    ins, stat, out_of_window = [], {}, 0
    cov = []
    for dev, vend, region, version, android, spl, _ in latest:
        dtier = tier_of(spl)
        dfloor = floor.get(dev, spl)
        per, skipped = {}, 0
        for cve, (fix_spl, ctier, osv, sev, vref, asb) in cves.items():
            in_win = 1 if _months_between(fix_spl, dfloor) <= relevance_months else 0
            if not in_win:
                out_of_window += 1; skipped += 1
                continue
            prec = spl_precision(spl)
            if prec == "month":
                # Sound across months, ambiguous within one: we cannot tell whether
                # the vendor means -01 or -05, and for a same-month fix that is
                # exactly the distinction that decides the verdict.
                if spl[:7] > fix_spl[:7]:
                    st = "claimed-fixed"
                elif spl[:7] < fix_spl[:7]:
                    st = "open"
                else:
                    st = "unadjudicable-month-precision"
            elif dtier is None:
                st = "unknown-untiered-spl"
            elif dtier < ctier:
                # e.g. a -01 build against a kernel/chipset (-05) CVE. This is the
                # same guard the chipset lane applies, stated once and generally.
                st = "unadjudicable-tier"
            elif spl >= fix_spl:
                st = "claimed-fixed"
            else:
                st = "open"
            stat[st] = stat.get(st, 0) + 1
            per[st] = per.get(st, 0) + 1
            # A 'claimed-fixed' row is a denominator, not a finding. Stored per-row it
            # was 585,241 of 656,178 rows — 89% of the table saying "this 2026 phone
            # is fixed against a 2025 bug". Keep the COUNT (a reader needs it to know
            # the check ran and against what) and store only what needs acting on.
            if st != "claimed-fixed":
                ins.append((dev, vend, region, version, android, spl, dtier, cve, ctier,
                            fix_spl, sev, osv, vref, asb, st, in_win))
        cov.append((dev, region, version, android, spl, dtier,
                    per.get("open", 0), per.get("claimed-fixed", 0),
                    sum(v for k, v in per.items()
                        if k.startswith(("unadjudicable", "unknown"))),
                    sum(per.values()), relevance_months, skipped))
    con.executemany("INSERT OR REPLACE INTO platform_vuln VALUES(" + ",".join("?" * 16) + ")",
                    ins)
    con.executemany("INSERT OR REPLACE INTO platform_coverage VALUES(" + ",".join("?" * 12) + ")",
                    cov)
    con.commit()

    if verbose:
        nd = con.execute("SELECT COUNT(DISTINCT device) FROM platform_vuln").fetchone()[0]
        ndev_chip = con.execute("SELECT COUNT(DISTINCT device) FROM device_vuln").fetchone()[0] \
            if con.execute("SELECT name FROM sqlite_master WHERE name='device_vuln'").fetchone() else 0
        print(f"platform_vuln: {len(ins):,} actionable rows stored "
              f"({stat.get('claimed-fixed',0):,} claimed-fixed kept as counts, not rows) "
              f"over {len(latest):,} builds · "
              f"{nd} devices (chipset lane reaches {ndev_chip})")
        print(f"  {out_of_window:,} (device, CVE) pairs skipped as out of the "
              f"{relevance_months}-month relevance window")
        for k in ("open", "claimed-fixed", "unadjudicable-tier", "unknown-untiered-spl"):
            if stat.get(k):
                print(f"  {k:22} {stat[k]:8,}")
        # The headline must be at DEVICE grain with a denominator: row grain inflates
        # by the region fan-out (measured 6.98x in this corpus) and reads as discovery.
        dc = con.execute("SELECT COUNT(DISTINCT device||'|'||cve) FROM platform_vuln "
                         "WHERE status='open'").fetchone()[0]
        tot_dev = con.execute("SELECT COUNT(DISTINCT device) FROM roms").fetchone()[0]
        print(f"  HEADLINE: {dc:,} distinct (device, CVE) open pairs across {nd} devices "
              f"— of {tot_dev:,} devices tracked ({100*nd//max(tot_dev,1)}% have a patch level)")
    if own:
        con.close()
    return stat


def for_device(con, device: str):
    cur = con.execute(
        "SELECT cve, cve_tier, fix_spl, spl, severity, status, asb_url, "
        "       GROUP_CONCAT(DISTINCT region) AS regions "
        "FROM platform_vuln WHERE device=? GROUP BY cve, fix_spl "
        "ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'unadjudicable-tier' THEN 1 ELSE 2 END, "
        "         fix_spl DESC", (device,))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def summary(con, vendor: str = "", only_open: bool = False):
    q = """SELECT device, vendor, region, version, android, spl, spl_tier,
                  SUM(status='open') open_n,
                  0 AS fixed_n,   -- see platform_coverage; not stored per row
                  SUM(status LIKE 'unadjudicable%' OR status LIKE 'unknown%') unknown_n,
                  COUNT(*) total
           FROM platform_vuln WHERE 1=1 """
    a = []
    if vendor:
        q += "AND vendor=? "; a.append(vendor)
    q += "GROUP BY device, region, version "
    if only_open:
        q += "HAVING open_n > 0 "
    q += "ORDER BY open_n DESC, device"
    cur = con.execute(q, a)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--device")
    ap.add_argument("--months", type=int, default=RELEVANCE_MONTHS,
                    help="relevance window in months (default 24)")
    a = ap.parse_args()
    con = sqlite3.connect(DB_PATH)
    if a.build or not a.device:
        build(con, relevance_months=a.months)
        n = con.execute("SELECT COUNT(*) FROM platform_vuln").fetchone()[0]
        log_run("platform-join", n, outcome="ok" if n else "empty")
    if a.device:
        rs = for_device(con, a.device)
        print(f"\n{a.device}: {len(rs)} platform CVE verdicts")
        for r in rs[:30]:
            print(f"  {r['status']:20} {r['cve']:18} tier-{r['cve_tier']} "
                  f"fixed_at={r['fix_spl']} build={r['spl']} {r['severity'] or ''}")
    con.close()


if __name__ == "__main__":
    main()
