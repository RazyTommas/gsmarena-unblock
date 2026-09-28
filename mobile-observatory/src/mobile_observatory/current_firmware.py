"""Build and publish the device_current_firmware projection.

`v_latest_firmware` is correct and unusably placed. It derives "latest" at read
time by json_extract()ing observations.payload_json across a four-table join and
then ranking with window functions, which costs 365ms on the live corpus --
303ms even for LIMIT 1, and 368ms for a single device, because a window over the
whole corpus has to be computed before any row can be discarded. Three indexes
and ANALYZE moved that to 324ms; it is not an index problem. At the assumed 200x
production volume the same derivation is ~41s per request, and /devices joins it
on every call.

It is also only a quarter of the answer. firmware_releases is Samsung-only, so
v_latest_firmware covers 82 of 303 canonical devices and the other 221 rendered
as "firmware not observed" -- false for 156 of them, whose firmware sits in
product_firmware_releases.

So this module does not memoise the view. It builds the projection the view
should have been, over both fact tables, and publishes it atomically.

SELECTION RULES, one per layer, never blended:

  canonical (firmware_releases, samsung.fota)
      Keeps v_latest_firmware's own ranking. On the live corpus 726 of 726 rows
      resolve to source_manifest_latest; the vendor_release_date and
      observation_order_only branches are unreachable there, and are retained
      only because a future capture without manifest positions would need them.

  evidence (product_firmware_releases)
      Ranks by the source's stated release date where there is one, else by
      observation order, and records which. This matters: of 24,650 rows only
      4,880 carry a real date, all from xiaomi.community.firmware_tracker.
      mifirm.community.firmware_archive contributes 19,648 rows with no date at
      all, so for those partitions "latest" is capture order and says so.

DATES ARE NEVER COMPARED ACROSS PUBLISHERS. Ranking happens inside one
(device, target, channel) partition, and inside it the FIRST key is the source's
currency_rank, so every subsequent comparison -- including every date -- is
between rows of the same publisher.

That ordering is stored on the source rather than written into the query
because it is a judgement: google.ota.checkin says what Google's servers would
hand the device today and cannot name a withdrawn build, while frbox, naijarom
and mifirm are archives where a row means a build existed, not that it ships.
Sources sharing a rank are treated as incomparable, and _validate() refuses to
publish a partition that mixes two of them rather than falling through to
comparing their dates.

Before the Transsion promotion, all 545 partitions drew on a single source and
this never came up; promoting 388 devices created 36 that draw on two, which is
how the rule got tested rather than merely asserted.

PUBLICATION is a build-then-swap. Rows are assembled in a staging table that
carries the same constraints, validated there, and only then moved across in one
transaction. Under WAL a concurrent reader sees either the whole previous
generation or the whole next one; there is no window in which a device has half
its targets. A failed validation leaves the previous generation serving.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from .database import Database

# A real yyyy-mm-dd, not the literal string 'null' (6 rows carry that) and not a
# partial date. Used to decide whether a stated date can be ranked on at all.
_ISO_DATE = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"

# Shred samsung.doc.aspl into a build -> patch level table. Cross-source by
# construction: this publisher describes builds another publisher ships, so the
# source and observation ride along on every row.
PATCH_LEVEL_SQL = f"""
INSERT INTO build_security_patch_levels
  (build_id,security_patch_level,patch_tier,source_id,observation_id,built_at)
SELECT json_extract(o.payload_json,'$.data.build'),
       json_extract(o.payload_json,'$.data.aspl_date'),
       json_extract(o.payload_json,'$.data.patch_tier'),
       o.source_id,
       min(o.id),
       ?
  FROM observations o
 WHERE o.source_id='samsung.doc.aspl'
   AND json_extract(o.payload_json,'$.data.build') IS NOT NULL
   AND json_extract(o.payload_json,'$.data.aspl_date') GLOB '{_ISO_DATE}'
 GROUP BY json_extract(o.payload_json,'$.data.build'), o.source_id
"""

CANONICAL_SQL = f"""
INSERT INTO device_current_firmware_staging
  (hardware_model_id,target_key,channel,fact_layer,firmware_release_id,
   product_firmware_release_id,source_id,build_id,android_version,android_major,
   security_patch_level,security_patch_level_source_id,
   effective_at,effective_at_basis,latest_basis,release_count)
SELECT lf.hardware_model_id,
       coalesce(ft.target_code,''),
       lf.channel,
       'canonical',
       lf.firmware_release_id,
       NULL,
       (SELECT min(o.source_id) FROM firmware_release_evidence fre
          JOIN evidence e ON e.id=fre.evidence_id
          JOIN observations o ON o.id=e.observation_id
         WHERE fre.firmware_release_id=lf.firmware_release_id),
       fr.build_id,
       os.display_name,
       os.major,
       -- The release's own column first; it is empty for every samsung.fota row
       -- today, but a source that does state a patch level must win over a
       -- third party's claim about the same build.
       coalesce(fr.security_patch_level, bspl.security_patch_level),
       CASE WHEN fr.security_patch_level IS NOT NULL THEN NULL
            ELSE bspl.source_id END,
       -- source_manifest_latest ranks on when WE observed the manifest declare
       -- it latest, so the date it carries is an observation time. Anything
       -- else falls back to vendor_released_at, which for this source is a
       -- month parsed out of the build identifier.
       CASE WHEN lf.latest_basis='source_manifest_latest'
            THEN lf.declared_latest_at ELSE fr.vendor_released_at END,
       CASE WHEN lf.latest_basis='source_manifest_latest' THEN 'source_observed'
            WHEN fr.vendor_released_at IS NULL THEN 'not_captured'
            ELSE 'build_identifier_month' END,
       lf.latest_basis,
       (SELECT count(*) FROM firmware_releases sib
         WHERE sib.hardware_model_id=lf.hardware_model_id
           AND ifnull(sib.firmware_target_id,'')=ifnull(lf.firmware_target_id,'')
           AND sib.channel=lf.channel)
  FROM v_latest_firmware lf
  JOIN firmware_releases fr ON fr.id=lf.firmware_release_id
  LEFT JOIN firmware_targets ft ON ft.id=lf.firmware_target_id
  LEFT JOIN os_releases os ON os.id=fr.os_release_id
  LEFT JOIN build_security_patch_levels bspl ON bspl.build_id=fr.build_id
"""

EVIDENCE_SQL = f"""
INSERT INTO device_current_firmware_staging
  (hardware_model_id,target_key,channel,fact_layer,firmware_release_id,
   product_firmware_release_id,source_id,build_id,android_version,android_major,
   security_patch_level,security_patch_level_source_id,
   effective_at,effective_at_basis,latest_basis,release_count)
WITH dated AS (
  SELECT pfr.*,
         -- NULLIF strips the 6 rows storing the literal string 'null', which is
         -- absence written as a value and must not rank as a date.
         CASE WHEN nullif(pfr.vendor_released_at,'null') GLOB '{_ISO_DATE}'
              THEN nullif(pfr.vendor_released_at,'null') END AS stated_at
    FROM product_firmware_releases pfr
   WHERE pfr.hardware_model_id IS NOT NULL
), ranked AS (
  SELECT dated.*,
         count(*) OVER (PARTITION BY hardware_model_id,region_code,channel) AS sibling_count,
         row_number() OVER (
           PARTITION BY hardware_model_id,region_code,channel
           -- Publisher currency first. Everything after it is a comparison
           -- BETWEEN ROWS OF THE SAME SOURCE, which is the only place a date
           -- comparison is meaningful: a frbox archive date and a Google OTA
           -- check-in date do not measure the same event, so they are separated
           -- by rank before either is looked at.
           ORDER BY (SELECT currency_rank FROM sources WHERE sources.id=dated.source_id),
                    stated_at IS NULL, stated_at DESC, created_at DESC, id DESC
         ) AS rank_in_partition
    FROM dated
)
SELECT hardware_model_id,
       region_code,
       channel,
       'evidence',
       NULL,
       id,
       source_id,
       build_id,
       android_version,
       android_major,
       -- No evidence-layer source states a patch level, and the ASPL table
       -- describes Samsung builds only. Recording the silence rather than
       -- reaching for an unrelated build's value.
       NULL,
       NULL,
       coalesce(stated_at, created_at),
       CASE WHEN stated_at IS NOT NULL THEN 'vendor_stated_date' ELSE 'source_observed' END,
       CASE WHEN stated_at IS NOT NULL THEN 'vendor_release_date' ELSE 'observation_order_only' END,
       sibling_count
  FROM ranked
 WHERE rank_in_partition=1
"""


IDENTITY_SQL = """
INSERT INTO device_catalog_flat_staging
  (hardware_model_id,manufacturer,brand,family,variant,model_code,codename,
   chip_marketing_name,chip_part_number,silicon_vendor,silicon_family,silicon_part_count)
SELECT dc.hardware_model_id,dc.manufacturer,dc.brand,dc.family,dc.variant,dc.model_code,dc.codename,
       chip.marketing_name, chip.part_number, chip.silicon_vendor, chip.silicon_family,
       ifnull(chip.part_count,0)
  FROM v_device_catalog dc
  LEFT JOIN (
    -- One row per device: the primary SoC. Ranked by role then part number so
    -- the choice is deterministic across rebuilds rather than whichever row the
    -- join happened to yield first. part_count travels with it so a device with
    -- several parts is never presented as having one.
    SELECT hardware_model_id, marketing_name, part_number, silicon_vendor, silicon_family, part_count
      FROM (SELECT hs.hardware_model_id, sp.marketing_name, sp.part_number,
                   sv.canonical_name AS silicon_vendor, sf.canonical_name AS silicon_family,
                   count(*) OVER (PARTITION BY hs.hardware_model_id) AS part_count,
                   row_number() OVER (PARTITION BY hs.hardware_model_id
                     ORDER BY CASE hs.role WHEN 'primary_soc' THEN 0 ELSE 1 END,
                              sp.part_number) AS rk
              FROM hardware_silicon hs
              JOIN silicon_parts sp ON sp.id = hs.part_id
              JOIN silicon_families sf ON sf.id = sp.family_id
              JOIN silicon_vendors sv ON sv.id = sf.vendor_id)
     WHERE rk = 1) chip ON chip.hardware_model_id = dc.hardware_model_id
"""


@dataclass(frozen=True)
class BuildReport:
    generation: int
    rows: int
    devices: int
    canonical_rows: int
    evidence_rows: int
    built_at: str
    digest: str

    def summary(self) -> str:
        return (f"device_current_firmware generation {self.generation}: {self.rows} rows "
                f"({self.canonical_rows} canonical, {self.evidence_rows} evidence) "
                f"covering {self.devices} devices")


class ProjectionError(RuntimeError):
    """Raised when a build fails validation. The previous generation keeps serving."""


def _mark_device_primary(connection) -> None:
    """Choose the one row per device the grid renders, and roll up its totals.

    This is the work devices_page used to do per request with a window function
    and a GROUP BY over the whole projection. Doing it once at build time is
    what keeps the grid a seek instead of a scan: measured on a 176x corpus,
    /devices goes from 1,549ms to 42ms.

    The ordering is the read path's own, moved rather than reinvented: basis
    first, so a device whose latest is genuinely established outranks one where
    it is a capture-order guess, and only then the date. Ordering by date first
    would let a confident old row lose to an uncertain new one.
    """
    connection.execute("""
        UPDATE device_current_firmware_staging SET
          is_device_primary=0, device_release_total=NULL,
          device_target_total=NULL, device_target_codes=NULL""")
    connection.execute("""
        WITH ranked AS (
          SELECT rowid AS rid,
                 row_number() OVER (
                   PARTITION BY hardware_model_id
                   ORDER BY
                     -- 1. How well "latest" is established. A guess must never
                     --    outrank a row whose source declared it.
                     CASE latest_basis
                       WHEN 'source_manifest_latest' THEN 0
                       WHEN 'vendor_release_date' THEN 1 ELSE 2 END,
                     -- 2. Which publisher is most current, then WHICH publisher.
                     --    Both come before any date, so the effective_at
                     --    comparison below is always between rows of ONE source.
                     --    Without this the device-level pick was ordering a
                     --    naijarom capture date against an frbox capture date --
                     --    274 devices span several publishers and 218 of them
                     --    were decided by exactly that comparison, which is the
                     --    thing this corpus is not allowed to do. Fixing it per
                     --    target in EVIDENCE_SQL and leaving it here meant the
                     --    rule held for every row except the one the grid shows.
                     (SELECT currency_rank FROM sources
                       WHERE sources.id = device_current_firmware_staging.source_id),
                     source_id,
                     effective_at DESC,
                     target_key, channel) AS rk
            FROM device_current_firmware_staging)
        UPDATE device_current_firmware_staging SET is_device_primary=1
         WHERE rowid IN (SELECT rid FROM ranked WHERE rk=1)""")
    connection.execute("""
        WITH totals AS (
          SELECT hardware_model_id AS hm, sum(release_count) AS releases,
                 -- DISTINCT, matching the codes beside it. count(*) counted
                 -- (target, channel) partitions while the list counted codes,
                 -- so 79 devices printed "11 regions" above a list of 6.
                 count(DISTINCT target_key) AS targets,
                 group_concat(DISTINCT target_key) AS codes
            FROM device_current_firmware_staging GROUP BY hardware_model_id)
        UPDATE device_current_firmware_staging AS s SET
          device_release_total=(SELECT releases FROM totals WHERE hm=s.hardware_model_id),
          device_target_total =(SELECT targets  FROM totals WHERE hm=s.hardware_model_id),
          device_target_codes =(SELECT codes    FROM totals WHERE hm=s.hardware_model_id)
         WHERE s.is_device_primary=1""")


def _validate(connection) -> None:
    """Refuse to publish a projection that is internally wrong.

    Every check here corresponds to a way this projection could lie to a user,
    not to a way SQLite could complain.
    """
    problems: list[str] = []

    # Empty output is only a fault when there was input. A corpus can hold
    # devices and no firmware at all -- a fresh catalogue, or a fixture that
    # seeds identities only -- and refusing to publish there would leave the
    # identity projection unbuilt and every one of those devices invisible.
    # What must never happen is producing nothing FROM something.
    rows = connection.execute("SELECT count(*) FROM device_current_firmware_staging").fetchone()[0]
    available = connection.execute(
        """SELECT (SELECT count(*) FROM firmware_releases)
                + (SELECT count(*) FROM product_firmware_releases
                    WHERE hardware_model_id IS NOT NULL)""").fetchone()[0]
    if rows == 0 and available:
        problems.append(f"the corpus holds {available} firmware rows for known devices but the "
                        "build produced none; refusing to publish an empty projection over them")

    # A partition drawing on two publishers would mean the ranking above compared
    # dates that do not share a definition.
    # A partition may draw on several publishers, PROVIDED currency_rank puts
    # them in a definite order -- then the winner is chosen by rank and no date
    # crosses a publisher boundary. Two sources sharing a rank in one partition
    # is the unresolvable case: the ranking would fall through to a date
    # comparison between publishers, which is exactly what must not happen.
    tied = connection.execute(
        """SELECT count(*) FROM (
             SELECT 1 FROM product_firmware_releases pfr
               JOIN sources s ON s.id = pfr.source_id
              WHERE pfr.hardware_model_id IS NOT NULL
              GROUP BY pfr.hardware_model_id, pfr.region_code, pfr.channel, s.currency_rank
             HAVING count(DISTINCT pfr.source_id) > 1)"""
    ).fetchone()[0]
    if tied:
        problems.append(
            f"{tied} (device,target,channel) partitions draw on several publishers that share a "
            "currency_rank, so the ranking would fall through to comparing their dates against "
            "each other; give them distinct ranks or leave the partition unresolved")

    # Every device the projection claims must exist, and every release it points
    # at must be the row it says it is.
    orphans = connection.execute(
        """SELECT count(*) FROM device_current_firmware_staging s
            WHERE NOT EXISTS (SELECT 1 FROM hardware_models hm WHERE hm.id=s.hardware_model_id)"""
    ).fetchone()[0]
    if orphans:
        problems.append(f"{orphans} staged rows name a hardware_model_id that does not exist")

    dangling = connection.execute(
        """SELECT count(*) FROM device_current_firmware_staging s
            WHERE (s.firmware_release_id IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM firmware_releases f WHERE f.id=s.firmware_release_id))
               OR (s.product_firmware_release_id IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM product_firmware_releases p WHERE p.id=s.product_firmware_release_id))"""
    ).fetchone()[0]
    if dangling:
        problems.append(f"{dangling} staged rows point at a release that does not exist")

    sides = connection.execute(
        """SELECT count(*) FROM device_current_firmware_staging
            WHERE (firmware_release_id IS NULL)=(product_firmware_release_id IS NULL)"""
    ).fetchone()[0]
    if sides:
        problems.append(f"{sides} staged rows set both or neither release pointer")

    # A row claiming a stated vendor date must actually carry one, or the UI
    # would print a capture time under a label promising a release date.
    mislabelled = connection.execute(
        f"""SELECT count(*) FROM device_current_firmware_staging
             WHERE effective_at_basis='vendor_stated_date'
               AND (effective_at IS NULL OR effective_at NOT GLOB '{_ISO_DATE}*')"""
    ).fetchone()[0]
    if mislabelled:
        problems.append(f"{mislabelled} staged rows claim a vendor-stated date without one")

    # No device may be served from both layers at once: the two tables are
    # disjoint today (measured overlap 0) and a future overlap needs a deliberate
    # precedence rule, not whichever INSERT ran last.
    both = connection.execute(
        """SELECT count(*) FROM (
             SELECT hardware_model_id FROM device_current_firmware_staging
              GROUP BY hardware_model_id HAVING count(DISTINCT fact_layer)>1)"""
    ).fetchone()[0]
    if both:
        problems.append(f"{both} devices are served from both fact layers; precedence is undefined")

    # A patch level with no publisher attached cannot be shown honestly, and an
    # attribution with no patch level is a dangling claim.
    unattributed = connection.execute(
        f"""SELECT count(*) FROM device_current_firmware_staging
             WHERE (security_patch_level IS NOT NULL
                    AND security_patch_level NOT GLOB '{_ISO_DATE}')
                OR (security_patch_level IS NULL
                    AND security_patch_level_source_id IS NOT NULL)"""
    ).fetchone()[0]
    if unattributed:
        problems.append(f"{unattributed} staged rows carry a malformed or unattributed patch level")

    # The identity projection must cover the catalogue EXACTLY. A device missing
    # here is invisible in the grid, not merely missing its firmware.
    catalogue = connection.execute("SELECT count(*) FROM hardware_models").fetchone()[0]
    flattened = connection.execute("SELECT count(*) FROM device_catalog_flat_staging").fetchone()[0]
    if catalogue != flattened:
        problems.append(f"device_catalog_flat_staging holds {flattened} rows for {catalogue} "
                        "hardware models; the grid would silently drop the difference")

    # Exactly one primary row per device, or the grid either drops a device or
    # renders it twice.
    bad_primary = connection.execute(
        """SELECT count(*) FROM (
             SELECT hardware_model_id FROM device_current_firmware_staging
              GROUP BY hardware_model_id HAVING sum(is_device_primary)<>1)"""
    ).fetchone()[0]
    if bad_primary:
        problems.append(f"{bad_primary} devices do not have exactly one primary row")

    if problems:
        raise ProjectionError("; ".join(problems))


def build(db: Database, *, verbose: bool = False) -> BuildReport:
    """Rebuild the projection and publish it atomically.

    Returns a report. Raises ProjectionError without touching the served table if
    the freshly built rows do not validate.
    """
    connection = db.connection
    built_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # ONE write transaction for the whole build, not just the swap.
    #
    # Staging used to be assembled in autocommit and only the swap was
    # transactional. That leaves three holes: two builders can interleave and
    # publish a generation assembled from different snapshots, both can compute
    # the same generation number, and a failure after the committed DELETE on
    # build_security_patch_levels leaves that table empty for whoever reads it
    # next. Holding the write lock for the whole build closes all three. Under
    # WAL it costs readers nothing -- they are never blocked by a writer -- and
    # the build is ~600ms on the live corpus.
    connection.execute("BEGIN IMMEDIATE")
    try:
        report = _build_locked(db, connection, built_at, verbose=verbose)
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return report


def _build_locked(db: Database, connection, built_at: str, *, verbose: bool) -> BuildReport:
    """The build itself, with the write lock already held."""
    # Patch levels first: the canonical select joins them.
    connection.execute("DELETE FROM build_security_patch_levels")
    connection.execute(PATCH_LEVEL_SQL, (built_at,))

    connection.execute("DELETE FROM device_catalog_flat_staging")
    connection.execute(IDENTITY_SQL)

    connection.execute("DELETE FROM device_current_firmware_staging")
    connection.execute(CANONICAL_SQL)
    connection.execute(EVIDENCE_SQL)

    _mark_device_primary(connection)
    _validate(connection)

    stats = connection.execute(
        """SELECT count(*),
                  count(DISTINCT hardware_model_id),
                  sum(fact_layer='canonical'),
                  sum(fact_layer='evidence')
             FROM device_current_firmware_staging"""
    ).fetchone()
    rows, devices, canonical_rows, evidence_rows = (stats[0], stats[1], stats[2] or 0, stats[3] or 0)

    # A digest of what was published, so a later reader can tell two generations
    # apart without diffing the table.
    # Every column a reader can SEE, not just the key and the build. The earlier
    # digest omitted the date, the patch level, the Android version, the source
    # and the primary choice, so a correction that changed what the grid
    # displays republished under an identical digest -- a fingerprint that
    # cannot tell two different pages apart is worse than none, because it is
    # trusted. The identity projection is folded in for the same reason.
    #
    # Built from two ordered queries rather than one UNION: SQLite rejects an
    # ORDER BY inside a compound branch, and an unordered digest is not a digest.
    digester = hashlib.sha256()
    for row in connection.execute(
            """SELECT hardware_model_id, target_key, channel, build_id, latest_basis,
                      ifnull(source_id,''), ifnull(effective_at,''), effective_at_basis,
                      ifnull(android_version,''), ifnull(security_patch_level,''),
                      ifnull(security_patch_level_source_id,''), release_count, is_device_primary
                 FROM device_current_firmware_staging
                ORDER BY hardware_model_id, target_key, channel"""):
        digester.update(("|".join(str(value) for value in row) + "\n").encode("utf-8"))
    for row in connection.execute(
            """SELECT hardware_model_id, brand, variant, model_code,
                      ifnull(chip_part_number,''), ifnull(silicon_vendor,'')
                 FROM device_catalog_flat_staging
                ORDER BY hardware_model_id"""):
        digester.update(("identity|" + "|".join(str(value) for value in row) + "\n").encode("utf-8"))
    digest = digester.hexdigest()

    previous = connection.execute(
        "SELECT generation FROM projection_state WHERE name='device_current_firmware'").fetchone()
    generation = (previous[0] if previous else 0) + 1

    # The swap, inside the transaction opened by build(). A reader under WAL
    # sees either the whole previous generation or the whole next one.
    if True:
        txn = connection
        # Both projections swap together. Publishing them separately would let a
        # reader see identity from one generation beside firmware from another.
        txn.execute("DELETE FROM device_catalog_flat")
        txn.execute("""INSERT INTO device_catalog_flat
                         (hardware_model_id,manufacturer,brand,family,variant,model_code,codename,
                          chip_marketing_name,chip_part_number,silicon_vendor,silicon_family,silicon_part_count)
                       SELECT hardware_model_id,manufacturer,brand,family,variant,model_code,codename,
                              chip_marketing_name,chip_part_number,silicon_vendor,silicon_family,silicon_part_count
                         FROM device_catalog_flat_staging""")
        txn.execute("DELETE FROM device_current_firmware")
        txn.execute(
            """INSERT INTO device_current_firmware
                 (hardware_model_id,target_key,channel,fact_layer,firmware_release_id,
                  product_firmware_release_id,source_id,build_id,android_version,android_major,
                  security_patch_level,security_patch_level_source_id,
                  effective_at,effective_at_basis,latest_basis,release_count,
                  is_device_primary,device_release_total,device_target_total,device_target_codes)
               SELECT hardware_model_id,target_key,channel,fact_layer,firmware_release_id,
                      product_firmware_release_id,source_id,build_id,android_version,android_major,
                      security_patch_level,security_patch_level_source_id,
                      effective_at,effective_at_basis,latest_basis,release_count,
                      is_device_primary,device_release_total,device_target_total,device_target_codes
                 FROM device_current_firmware_staging""")
        txn.execute(
            """INSERT INTO projection_state
                 (name,generation,built_at,row_count,source_digest,input_fingerprint)
               VALUES('device_current_firmware',?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 generation=excluded.generation, built_at=excluded.built_at,
                 row_count=excluded.row_count, source_digest=excluded.source_digest,
                 input_fingerprint=excluded.input_fingerprint""",
            (generation, built_at, rows, digest, input_fingerprint(connection)))

    connection.execute("DELETE FROM device_current_firmware_staging")
    connection.execute("DELETE FROM device_catalog_flat_staging")
    report = BuildReport(generation, rows, devices, canonical_rows, evidence_rows, built_at, digest)
    if verbose:
        print(report.summary())
    return report


def main() -> None:
    """Rebuild the projection on its own.

    run_batch rebuilds after every ingest, but an operator who has just restored
    a corpus, applied a migration, or corrected data by hand needs to republish
    without replaying a whole batch -- and a corpus whose projection was never
    built reports every device as having no firmware.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Rebuild the current-firmware serving projection")
    parser.add_argument("--data-dir", default=".observatory-data")
    args = parser.parse_args()

    from pathlib import Path
    db = Database.migrated(Path(args.data_dir) / "corpus.sqlite")
    try:
        previous = state(db.connection)
        report = build(db)
        print(report.summary())
        print(f"  published generation {report.generation}"
              f" (was {previous['generation'] if previous else 'never built'})")
        print(f"  digest {report.digest[:16]}"
              f"{' — unchanged' if previous and previous['digest'] == report.digest else ' — changed'}")
    except ProjectionError as error:
        raise SystemExit(f"projection NOT published: {error}")
    finally:
        db.close()


# The tables the projection is derived from. A change in any of them can change
# what the projection would produce, and none of them necessarily changes an
# observation's observed_at.
_INPUT_TABLES = ("hardware_models", "device_variants", "device_families", "brands",
                 "manufacturers", "firmware_releases", "product_firmware_releases",
                 "hardware_silicon", "silicon_parts", "sources", "observations",
                 "firmware_release_evidence", "evidence", "product_hardware_links",
                 "source_products")


def input_fingerprint(connection) -> str:
    """What the projection would be built from, as one comparable value.

    Row counts plus the latest stamp each table carries. It cannot see an edit
    that changes a value in place without touching either -- a corrected region
    code on an existing row, say -- so it is a staleness DETECTOR, not a proof
    of freshness. It catches every case the wall-clock comparison missed:
    promotions, new devices, re-ingests, rank changes and row-level corrections
    that add or remove anything.
    """
    digester = hashlib.sha256()
    # How many firmware rows are ATTACHED to a device, not just how many exist.
    # Withdrawing a rejected identity sets hardware_model_id back to NULL in
    # place: no row is added or removed and no timestamp moves, so counts and
    # max() alone could not see it. Reproduced before fixing -- a reviewer
    # rejected an identity, the grid kept serving its 332 builds, and the
    # fingerprint was byte-identical before and after.
    digester.update(("attached:%d\n" % connection.execute(
        "SELECT count(*) FROM product_firmware_releases WHERE hardware_model_id IS NOT NULL"
    ).fetchone()[0]).encode("utf-8"))
    for table in _INPUT_TABLES:
        count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        stamp = ""
        for column in ("updated_at", "created_at", "observed_at"):
            has = any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))
            if has:
                stamp = connection.execute(
                    f"SELECT ifnull(max({column}),'') FROM {table}").fetchone()[0]
                break
        digester.update(f"{table}:{count}:{stamp}\n".encode("utf-8"))
    return digester.hexdigest()


def state(connection) -> dict | None:
    """What is currently published, or None if the projection has never been built."""
    row = connection.execute(
        "SELECT generation,built_at,row_count,source_digest FROM projection_state "
        "WHERE name='device_current_firmware'").fetchone()
    if row is None:
        return None
    return {"generation": row[0], "builtAt": row[1], "rowCount": row[2], "digest": row[3]}


if __name__ == "__main__":
    main()
