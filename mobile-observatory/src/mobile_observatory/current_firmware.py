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
from .source_dates import ISO_DATE_GLOB as _ISO_DATE

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
 -- firmware_targets is UNIQUE(vendor_namespace,target_code), so ONE code can
 -- legitimately exist under several namespaces. v_latest_firmware partitions by
 -- firmware_target_id; this projection is keyed on the target CODE, because the
 -- code is what gets shown to a reader as the region. Two namespaces claiming
 -- the same code for one device therefore produced two rows with one key, and
 -- the staging table's primary key turned that into
 --   UNIQUE constraint failed: ..hardware_model_id, ..target_key, ..channel
 -- which killed the whole batch -- every device's projection lost to one
 -- device's ambiguity. That is the same failure already fixed once for tied
 -- partitions, and it gets the same answer: refuse the PARTITION, not the run.
 --
 -- Refusing rather than picking is the honest option. Which namespace's build
 -- is "the" build for a region label is exactly what is unknown, and choosing
 -- one silently is the class of falsehood this projection exists to remove.
 -- The excluded group is reported by integrity.check_corpus.
 WHERE NOT EXISTS (
   SELECT 1 FROM v_latest_firmware peer
     LEFT JOIN firmware_targets pft ON pft.id = peer.firmware_target_id
    WHERE peer.hardware_model_id = lf.hardware_model_id
      AND peer.channel = lf.channel
      AND coalesce(pft.target_code,'') = coalesce(ft.target_code,'')
      AND ifnull(peer.firmware_target_id,'') <> ifnull(lf.firmware_target_id,''))
"""

EVIDENCE_SQL = f"""
INSERT INTO device_current_firmware_staging
  (hardware_model_id,target_key,channel,fact_layer,firmware_release_id,
   product_firmware_release_id,source_id,build_id,android_version,android_major,
   security_patch_level,security_patch_level_source_id,
   effective_at,effective_at_basis,latest_basis,release_count)
WITH dated AS (
  SELECT pfr.*,
         -- The nullif(...,'null') that used to sit inside this expression is gone.
         -- It was one of five copies of a sentinel guard bolted onto READERS while
         -- the two places that DERIVE the value went on storing the sentinel; the
         -- rule now lives in source_dates and is applied where the value is written
         -- (enrichment.promote_approved_product_observations) and where it is
         -- generated (observations.effective_at, migration 0031), so
         -- vendor_released_at is either a date or NULL. The shape test stays,
         -- because a stated date must still be a FULL date to be ranked on -- a
         -- '2024-05' would sort before every day in May.
         CASE WHEN pfr.vendor_released_at GLOB '{_ISO_DATE}'
              THEN pfr.vendor_released_at END AS stated_at
    FROM product_firmware_releases pfr
   WHERE pfr.hardware_model_id IS NOT NULL
     -- A build whose identifier names a LONGER model than the device it is
     -- attached to belongs to a sibling: "i3Pro-..." on the device "i3". The
     -- source filed it under this code and the join is faithful to that, but it
     -- is not evidence about THIS phone, so it must never be the answer to
     -- "what is it running". Measured before this: 5 devices had exactly such a
     -- build as their headline row, including TECNO i3, W3, i5, W5 and itel
     -- S11 -- each told a user its current firmware was another model's ROM.
     --
     -- Excluded from the SELECTION only. The row stays in
     -- product_firmware_releases and in the device's ROM history, attributed to
     -- the source that filed it, because deleting a source's statement is not
     -- this projection's job. Four of the five devices have a genuine build to
     -- fall back on; the fifth then reports no firmware, which is true.
     AND NOT EXISTS (
       SELECT 1 FROM device_catalog_flat_staging d
        WHERE d.hardware_model_id = pfr.hardware_model_id
          AND instr(pfr.build_id,'-') > 1
          AND lower(substr(pfr.build_id, 1, instr(pfr.build_id,'-') - 1)) LIKE
              lower(replace(replace(replace(d.model_code, d.brand || ' ', ''),
                                    d.brand || '-', ''), ' ', '')) || '_%')
     -- A partition whose publishers TIE on currency_rank is unresolvable: the
     -- ranking would fall through to comparing their dates with each other,
     -- which is the one thing this corpus must not do. It is left unanswered
     -- rather than guessed.
     --
     -- Scoped to the partition, deliberately. This used to fail the whole
     -- build, and on a corpus rebuilt from scratch -- where every source is
     -- ingested in one pass and overlaps are commonest -- 47 tied partitions
     -- published NOTHING: 854 devices, zero with firmware. Refusing to answer
     -- one question is honest; refusing to answer any because one is
     -- ambiguous is a different and worse failure.
     AND NOT EXISTS (
       SELECT 1 FROM product_firmware_releases tie
         JOIN sources ts ON ts.id = tie.source_id
         JOIN sources ms ON ms.id = pfr.source_id
        WHERE tie.hardware_model_id = pfr.hardware_model_id
          AND tie.region_code = pfr.region_code
          AND tie.channel = pfr.channel
          AND tie.source_id <> pfr.source_id
          AND ts.currency_rank = ms.currency_rank)
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


#: The device-level pick's ordering keys: (recorded name, SQL, direction).
#:
#: ONE definition. `row_number()` chooses the winner from it and the derivation
#: below records WHICH key separated that winner from the runner-up, so the pick
#: and the stated reason for the pick cannot disagree -- which is this codebase's
#: recorded lesson #2, and the reason this is a table rather than two ORDER BYs.
#: `{t}` is the row's table or alias.
#:
#: THE ORDER OF THESE KEYS IS THE WHOLE RULE. Read down:
#:
#: 1. `latest_basis` -- how well "latest" is established. A capture-order guess
#:    must never outrank a row whose source declared it, so this comes before any
#:    date: ordering by date first would let a confident old row lose to an
#:    uncertain new one.
#:
#: 2. `publisher_currency_rank` -- a DECLARED judgement, recorded by a human on
#:    the source row, that two publishers' dates do not measure the same event.
#:    google.ota.checkin says what Google's servers would hand the device today;
#:    an archive row says a build once existed, and 2026-09-30 from the archive is
#:    not "later" than 2026-01-01 from the check-in because they answer different
#:    questions. This stays AHEAD of the date, and that is not the defect being
#:    fixed: a rank is a statement about meaning, where `source_id` ascending is a
#:    statement about the alphabet.
#:
#:    Learned the hard way here. This key was moved BELOW the date first, and
#:    `test_the_most_current_publisher_wins_regardless_of_date` -- the guard the
#:    previous round wrote for the 218 devices -- failed immediately. It was right
#:    to.
#:
#: 3. `latest_stated_date` -- THE DATE, and it comes before the publisher's NAME.
#:
#:    This is the change. Two things gate it, and both are the argument:
#:
#:    * it is NULL in every row outside `latest_basis='vendor_release_date'`, so
#:      it is inert for any basis where `effective_at` is a capture time. Inside
#:      that basis `effective_at` IS one kind of measurement -- measured on the
#:      live corpus, `latest_basis='vendor_release_date'` and
#:      `effective_at_basis='vendor_stated_date'` are the same 536 rows (0
#:      disagreements either way), all 536 carrying a full 10-character ISO date,
#:      0 NULL;
#:    * key 2 has already separated publishers whose dates are declared
#:      incomparable, so by the time this key is read the comparison is between
#:      two publishers the corpus has NO basis to distinguish -- and then the
#:      later vendor-stated release date is simply the more recent build.
#:
#:    "Current firmware" means the most recent one, which is derivable from the
#:    evidence, so no authority judgement is needed and none is made. Measured:
#:    this takes the headline changes from 42 to 37 and the devices showing an
#:    EARLIER stated date than the build they replaced from 4 to 0.
#:
#: 4. `publisher_identity` -- the publisher's NAME, gated to the complement of
#:    key 3: outside the vendor-stated-date basis the dates are not comparable, so
#:    something must fix the publisher before key 6 looks at a date, and this is
#:    it. Inside that basis the expression is NULL in every row and the name never
#:    enters the comparison at all. Where this key decides a device,
#:    `check_corpus` counts it -- 89 on the live corpus, all in the capture-order
#:    basis, down from the 243 that were merely at risk of it.
#:
#: 5. `android_version` -- at the same stated release date, the higher Android
#:    major is the more current software state. Derivable from the evidence, not
#:    from a name. Earns its place: it is what separates one real tie on this
#:    corpus (`cd216c76…`, two rows at 2025-05-05 with different majors).
#:
#: 6. `observation_order` -- capture order, and by now always within ONE publisher
#:    because keys 2 and 4 fixed the publisher for every basis where this key can
#:    still discriminate.
#:
#: 7. `arbitrary_stable_order` / `one_publishers_region_choice` -- the row's own
#:    coordinates. Stable across rebuilds and meaningless, which is why reaching
#:    it is RECORDED as a confession and not as a reason. See
#:    PRIMARY_BASIS_ONE_PUBLISHER_REGION for why the last key reports two
#:    different things.
#:
#: `sources.currency_rank` is `INTEGER NOT NULL DEFAULT 50`, so key 2 cannot be
#: NULL and a new source cannot silently win every tie by having no rank.
_PRIMARY_KEYS: tuple[tuple[str, str, str], ...] = (
    ("latest_basis",
     "CASE {t}.latest_basis WHEN 'source_manifest_latest' THEN 0"
     " WHEN 'vendor_release_date' THEN 1 ELSE 2 END", "ASC"),
    ("publisher_currency_rank",
     "(SELECT currency_rank FROM sources WHERE sources.id = {t}.source_id)", "ASC"),
    ("latest_stated_date",
     "CASE WHEN {t}.latest_basis='vendor_release_date' THEN {t}.effective_at END", "DESC"),
    ("publisher_identity",
     "CASE WHEN {t}.latest_basis!='vendor_release_date' THEN {t}.source_id END", "ASC"),
    ("android_version",
     "CASE WHEN {t}.latest_basis='vendor_release_date' THEN {t}.android_major END", "DESC"),
    ("observation_order", "{t}.effective_at", "DESC"),
    ("arbitrary_stable_order", "{t}.target_key || char(31) || {t}.channel", "ASC"),
)

#: Reached when the last key is what separated them AND the runner-up is the SAME
#: publisher's row: the pick is then which of ONE publisher's region/channel
#: partitions to show, and the grid already prints the region it chose and how
#: many the device has. Split out from `arbitrary_stable_order` because the two
#: are not the same problem and the counts are not close: measured, 364 of the 365
#: devices that reach the last key are this, and exactly 1 is a genuine
#: cross-publisher tie. Reporting 365 unresolvable ties would be a true sentence
#: that reads as a far worse fact than the one it describes.
PRIMARY_BASIS_ONE_PUBLISHER_REGION = "one_publishers_region_choice"

#: Values of `device_primary_basis`, plus the two the keys above cannot name on
#: their own. Keep in step with the CHECK constraint in
#: migrations/0035_device_primary_basis.sql; the test asserts they agree.
PRIMARY_BASES: tuple[str, ...] = (
    ("sole_candidate",) + tuple(k[0] for k in _PRIMARY_KEYS)
    + (PRIMARY_BASIS_ONE_PUBLISHER_REGION,))

#: The pick is decided by the publisher's NAME in exactly this state. Named here
#: so integrity.check_corpus counts the same thing the projection records.
PRIMARY_BASIS_DECIDED_BY_NAME = "publisher_identity"


def _primary_order_by(table: str) -> str:
    return ",\n                     ".join(
        f"{sql.format(t=table)} {direction}" for _, sql, direction in _PRIMARY_KEYS)


def _primary_basis_case(winner: str, runner_up: str) -> str:
    """SQL naming the first key on which `winner` and `runner_up` differ.

    `IS NOT` and not `!=`: four of the seven keys are deliberately NULL outside
    the basis they apply to, and `NULL != NULL` is NULL, which a CASE treats as
    false -- so a `!=` here would fall through every gated key and report the last
    one for every device. Null-safe comparison is what makes the gating legible
    instead of invisible. Verified against SQLite rather than assumed: `||` binds
    tighter than `IS NOT`, `NULL IS NOT NULL` is 0, and `NULL != NULL` is NULL.

    The last key is then refined by WHO the runner-up was -- see
    PRIMARY_BASIS_ONE_PUBLISHER_REGION. The inner CASE is emitted twice because a
    scalar subquery cannot correlate into its own FROM clause, but it is emitted
    from ONE expression here, so the two copies cannot drift.
    """
    branches = "".join(
        f"\n               WHEN {sql.format(t=winner)} IS NOT {sql.format(t=runner_up)}"
        f" THEN '{name}'"
        for name, sql, _ in _PRIMARY_KEYS)
    inner = f"CASE{branches}\n               ELSE 'arbitrary_stable_order' END"
    return (f"CASE WHEN ({inner}) = 'arbitrary_stable_order'"
            f"\n                 AND {winner}.source_id IS {runner_up}.source_id"
            f"\n            THEN '{PRIMARY_BASIS_ONE_PUBLISHER_REGION}'"
            f"\n            ELSE ({inner}) END")


def _mark_device_primary(connection) -> None:
    """Choose the one row per device the grid renders, and roll up its totals.

    This is the work devices_page used to do per request with a window function
    and a GROUP BY over the whole projection. Doing it once at build time is
    what keeps the grid a seek instead of a scan: measured on a 176x corpus,
    /devices goes from 1,549ms to 42ms.

    The ordering is `_PRIMARY_KEYS` and the reasoning for every key is there.
    What happens here is that the winner is chosen by it AND the key that
    actually separated the winner from the runner-up is recorded, so a reader can
    tell a pick the evidence made from a tie something had to break.
    """
    table = "device_current_firmware_staging"
    connection.execute("""
        UPDATE device_current_firmware_staging SET
          is_device_primary=0, device_release_total=NULL,
          device_target_total=NULL, device_target_codes=NULL,
          device_primary_basis=NULL""")
    connection.execute(f"""
        WITH ranked AS (
          SELECT rowid AS rid,
                 row_number() OVER (
                   PARTITION BY hardware_model_id
                   ORDER BY
                     {_primary_order_by(table)}) AS rk
            FROM {table})
        UPDATE device_current_firmware_staging SET is_device_primary=1
         WHERE rowid IN (SELECT rid FROM ranked WHERE rk=1)""")
    # The reason, derived from the SAME keys and written in the same pass over
    # the same staging rows. Ranked a second time rather than carried out of the
    # statement above because SQLite's UPDATE...FROM cannot see a window
    # function's neighbouring row, and re-deriving from `_PRIMARY_KEYS` cannot
    # drift from it the way a hand-copied ORDER BY would.
    connection.execute(f"""
        WITH ranked AS (
          SELECT rowid AS rid, hardware_model_id AS hm,
                 row_number() OVER (
                   PARTITION BY hardware_model_id
                   ORDER BY
                     {_primary_order_by(table)}) AS rk
            FROM {table}),
        pair AS (
          SELECT w.rid AS rid,
                 (SELECT r.rid FROM ranked r WHERE r.hm=w.hm AND r.rk=2) AS runner
            FROM ranked w WHERE w.rk=1)
        UPDATE device_current_firmware_staging AS s SET device_primary_basis = (
          SELECT CASE WHEN pair.runner IS NULL THEN 'sole_candidate' ELSE (
                   SELECT {_primary_basis_case('win', 'lose')}
                     FROM {table} win, {table} lose
                    WHERE win.rowid=pair.rid AND lose.rowid=pair.runner) END
            FROM pair WHERE pair.rid=s.rowid)
         WHERE s.is_device_primary=1""")
    connection.execute("""
        WITH totals AS (
          SELECT hardware_model_id AS hm, sum(release_count) AS releases,
                 -- DISTINCT, matching the codes beside it. count(*) counted
                 -- (target, channel) partitions while the list counted codes,
                 -- so 79 devices printed "11 regions" above a list of 6.
                 count(DISTINCT target_key) AS targets,
                 group_concat(DISTINCT target_key) AS codes,
                 count(DISTINCT source_id) AS publishers
            FROM device_current_firmware_staging GROUP BY hardware_model_id)
        UPDATE device_current_firmware_staging AS s SET
          device_release_total=(SELECT releases   FROM totals WHERE hm=s.hardware_model_id),
          device_target_total =(SELECT targets    FROM totals WHERE hm=s.hardware_model_id),
          device_target_codes =(SELECT codes      FROM totals WHERE hm=s.hardware_model_id),
          device_source_count =(SELECT publishers FROM totals WHERE hm=s.hardware_model_id)
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
    # Tied partitions are EXCLUDED by EVIDENCE_SQL rather than rejected here.
    # They are a question the corpus cannot answer, not a corruption, and
    # failing the build over them threw away every answer it could give.
    # integrity.check_corpus reports the count so they stay visible.

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

    # A pick with no recorded reason is the state this projection was in before
    # migration 0035, and it is the state a reader cannot audit: the grid shows
    # one build out of several and nothing says what chose it. Refusing here
    # rather than reporting it later, because the basis is derived in the same
    # pass as the pick -- a primary row without one means the derivation did not
    # run, not that the corpus is thin.
    unexplained = connection.execute(
        """SELECT count(*) FROM device_current_firmware_staging
            WHERE is_device_primary=1 AND device_primary_basis IS NULL"""
    ).fetchone()[0]
    if unexplained:
        problems.append(f"{unexplained} primary rows record no basis for having been chosen")

    # And the mirror: a basis on a row that was NOT chosen describes a choice
    # nobody made.
    stray = connection.execute(
        """SELECT count(*) FROM device_current_firmware_staging
            WHERE is_device_primary=0 AND device_primary_basis IS NOT NULL"""
    ).fetchone()[0]
    if stray:
        problems.append(f"{stray} non-primary rows carry a basis for a choice that was not made")

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
                      ifnull(security_patch_level_source_id,''), release_count, is_device_primary,
                      -- In the digest, because a generation where the same build
                      -- is shown for a DIFFERENT recorded reason is a different
                      -- page, and the comment above says a fingerprint that
                      -- cannot tell two pages apart is worse than none.
                      ifnull(device_primary_basis,'')
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
                  is_device_primary,device_release_total,device_target_total,device_target_codes,
                  device_source_count,device_primary_basis)
               SELECT hardware_model_id,target_key,channel,fact_layer,firmware_release_id,
                      product_firmware_release_id,source_id,build_id,android_version,android_major,
                      security_patch_level,security_patch_level_source_id,
                      effective_at,effective_at_basis,latest_basis,release_count,
                      is_device_primary,device_release_total,device_target_total,device_target_codes,
                      device_source_count,device_primary_basis
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
