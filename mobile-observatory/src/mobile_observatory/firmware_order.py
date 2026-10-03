"""How a published "this build came after that one" claim was ordered.

THE DEFECT THIS EXISTS FOR, measured 2026-10-04 on a read-only copy of the live
corpus.

`android_version_changed` pairs two `product_firmware_releases` rows and states
that one came after the other. The pair is chosen by walking the history in
`ORDER BY product_id, region_code, channel, vendor_released_at, id`. When
`vendor_released_at` is NULL on both rows the sort collapses onto `id`, and `id`
is `uuid5(_NS, "product-firmware" + observation_id)` -- a hash. So the sequence
is not chronology; it is whatever order the digests happened to fall in.

`promote_approved_product_observations` read `$.data.release_date` and
`$.data.branch`, which is one publisher's vocabulary.
`mifirm.community.firmware_archive` publishes the same two facts as
`$.data.vendor_released_at` and `$.data.channel`, so every one of its 21,845
promoted releases stored no date at all. Result:

    5,804  android_version_changed events published
    5,643  of them rest on a pair where BOTH releases are undated
       56  rest on a pair where both carry a real date
      105  cite release rows the corpus no longer holds

and, replaying the same derivation over the same corpus with the dates and
channels the sources stated all along, **5,588 of the 5,690 events whose inputs
still exist would not have been produced at all, and 1,064 that should exist were
never produced.** 102 are the same either way. Of the events that do exist, 278
assert a direction the real dates reverse.

WHY THE EVENTS ARE NOT RETRACTED. `domain_events` carries
`domain_events_no_update` and `domain_events_no_delete` triggers and the feed is
an append-only published record; deleting 5,588 published facts is a larger harm
than leaving them, and it is not a decision this code gets to make. What was
wrong was not that the claims exist but that a reader could not tell which kind
of claim they were looking at. So nothing is withdrawn and every event now
carries its basis.

WHY A SEPARATE TABLE AND NOT A COLUMN ON `domain_events`.

  * The table is STRICT and immutable by trigger. `ALTER TABLE ADD COLUMN` would
    leave all 5,804 existing rows NULL, and the only way to fill them is an
    UPDATE, which means dropping the immutability trigger to write to the table
    whose immutability it exists to guarantee. Not worth it, and not an agent's
    call.
  * Adding the column with a constant DEFAULT is worse: SQLite would make every
    existing row read that default without an UPDATE, which would stamp
    `observation_order_only` onto the 161 events that are not.
  * The basis is a fact about the MOMENT the order was decided, and it must not
    move afterwards. That is why `before_released_at` / `after_released_at` are
    stored as they stood at decision time rather than re-read from the releases.
    Migration 0034 backfills 21,845 release dates, so a basis DERIVED from
    today's `product_firmware_releases` would report every one of those 5,643
    hash-ordered events as `vendor_release_date` the moment the backfill landed --
    an old claim wearing a new event's evidence, which is precisely the mistake a
    reader must not be able to make.

VOCABULARY. `software_state_basis` (migration 0019) already distinguishes
`source_manifest_latest` / `vendor_release_date` / `observation_order_only`, and
`observation_order_only` is exactly what these events are, so the same words are
used rather than a second set meaning the same thing. One state is added:
`cited_releases_absent`, because 105 events cite releases the corpus no longer
holds and calling those `observation_order_only` would state something unmeasured.

WRITTEN WITH THE EVENT, NEVER AFTERWARDS. `record_ordering_basis` is called only
on the branch where the `domain_events` INSERT actually inserted. An event that
already exists is never given a basis later, because by then the releases may
have been re-dated and the answer would be about today rather than about the
decision. `check_corpus` reports an event with no basis row as an ERROR, so the
gap is loud instead of being filled in with a guess.
"""
from __future__ import annotations

import sqlite3

# Keep in step with the CHECK constraint in migrations/0034_firmware_order_basis.sql.
# tests/test_firmware_order_basis.py asserts the two agree rather than trusting
# this comment.
ORDERING_BASES = (
    # Both releases in the pair carried a date the source stated. The claim is
    # chronology.
    "vendor_release_date",
    # Neither did. The pair's order came from the uuid5 row id and the claim is
    # capture-artefact ordering, not chronology.
    "observation_order_only",
    # Exactly one did. SQLite sorts NULL first, so the undated row was treated as
    # earlier than every dated one -- a conclusion the evidence does not support.
    "mixed_dated_and_undated",
    # The corpus no longer holds the releases this event names, so its basis
    # cannot be established. Reported, never guessed.
    "cited_releases_absent",
)


def ordering_basis(before_released_at: str | None, after_released_at: str | None) -> str:
    """Which of ORDERING_BASES describes an order decided on these two dates."""
    if before_released_at is not None and after_released_at is not None:
        return "vendor_release_date"
    if before_released_at is None and after_released_at is None:
        return "observation_order_only"
    return "mixed_dated_and_undated"


def record_ordering_basis(connection: sqlite3.Connection, *, event_id: str,
                          before_release_id: str, after_release_id: str,
                          before_released_at: str | None, after_released_at: str | None,
                          recorded_at: str) -> str:
    """Freeze how one event's order was decided. INSERT OR IGNORE, deliberately.

    OR IGNORE and not OR REPLACE: a basis already recorded is the one that was
    true when the order was decided, and a later run looking at re-dated releases
    must not be able to overwrite it with today's answer.
    """
    basis = ordering_basis(before_released_at, after_released_at)
    connection.execute(
        """INSERT OR IGNORE INTO domain_event_ordering
             (event_id,ordering_basis,before_release_id,after_release_id,
              before_released_at,after_released_at,recorded_at)
           VALUES(?,?,?,?,?,?,?)""",
        (event_id, basis, before_release_id, after_release_id,
         before_released_at, after_released_at, recorded_at))
    return basis
