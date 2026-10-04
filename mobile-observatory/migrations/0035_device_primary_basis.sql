BEGIN;

-- Which build a device shows as "current" was decided alphabetically, and the
-- corpus did not say so.
--
-- `_mark_device_primary` ranked `latest_basis` -> `currency_rank` -> `source_id`,
-- with the date AFTER the publisher. That was right when it was written and for
-- the reason written beside it: `effective_at` means different things in
-- different rows -- a vendor release date in one, a capture time in another --
-- and ordering a naijarom capture date against an frbox capture date is exactly
-- what this corpus is not allowed to do. Putting the publisher first guaranteed
-- every date comparison stayed inside one publisher.
--
-- What it also did was make `source_id` ASCENDING the tiebreak whenever two
-- publishers share a `currency_rank`. Repairing the mifirm release dates
-- (migration 0034) moved 39 Xiaomi devices into precisely that state: their
-- mifirm rows used to be `observation_order_only` and lost on basis alone, and
-- once dated both candidates are `vendor_release_date`, both publishers are rank
-- 50, and `mifirm.` sorts before `xiaomi.`. **7 of those 39 ended up showing a
-- build with an EARLIER stated release date than the one it replaced.** Trading
-- an order decided by a hash for one decided by the alphabet is the same defect
-- in a new coat.
--
-- THE DISTINCTION THE OLD RULE MISSED. `effective_at` is not always the same
-- kind of measurement, but inside `latest_basis='vendor_release_date'` it always
-- is: measured on the live corpus, that basis and
-- `effective_at_basis='vendor_stated_date'` are the same 536 rows exactly (0
-- disagreements either way) and all 536 carry a full 10-character ISO date, 0
-- NULL. Two vendor-stated release dates DO measure the same event, so comparing
-- them is not a cross-publisher date comparison in the sense the old comment
-- forbids -- it is the only thing "most recent" can mean. So the date now comes
-- before the publisher's NAME, but only within that basis; for every other basis
-- the publisher still comes first and nothing about the 218-device problem the
-- old rule fixed is re-opened.
--
-- AND `currency_rank` STAYS AHEAD OF THE DATE. It is not the publisher's name: it
-- is a judgement a human already recorded, saying that two publishers' dates do
-- not measure the same event at all. google.ota.checkin says what the vendor's
-- servers would hand the device today; an archive row says a build once existed,
-- and 2026-09-30 from the archive is not "later" than 2026-01-01 from the
-- check-in. The date was put ahead of the rank first, and
-- `test_the_most_current_publisher_wins_regardless_of_date` -- the guard the
-- previous round wrote for those 218 devices -- failed immediately. It was right
-- to. What the date now outranks is `source_id` ASCENDING, and nothing else.
--
-- No authority judgement is made or needed: "current firmware" means the most
-- recent one, and that is derivable from the evidence. mifirm and the Xiaomi
-- tracker are both rank 50, so for all 39 devices this was about, the rank ties
-- and the dates decide.
--
-- AND THE PICK NOW RECORDS WHAT DECIDED IT. A tie broken is not the same fact as
-- a tie that never existed, and the reader could not tell. `device_primary_basis`
-- names the first ordering key that actually separated the winner from the
-- runner-up, in the same `*_basis` vocabulary `latest_basis` and
-- `effective_at_basis` already use. Two of its values are confessions rather
-- than reasons:
--
--   publisher_identity     -- the publisher's NAME decided it. Only reachable
--                             outside the vendor-stated-date basis, where the
--                             dates are not comparable. `check_corpus` counts
--                             these.
--   arbitrary_stable_order -- nothing above separated them and the runner-up was
--                             ANOTHER publisher's row. The corpus cannot say
--                             which build is current. Measured: exactly **1**
--                             device -- `OS1.0.2.0.TKVCNXM` against
--                             `V816.0.2.0.TKVCNXM`, same stated date, same
--                             Android major, which is one build under two of
--                             Xiaomi's own naming conventions. Collapsing those
--                             is an identity judgement and is not made here.
--
--   one_publishers_region_choice
--                          -- nothing above separated them and the runner-up was
--                             the SAME publisher's row for a different
--                             region/channel. The pick is which partition to put
--                             on the grid, and the grid already prints the region
--                             it chose beside the device's region count. 364
--                             devices, mostly frbox (285) and samsung.fota (72),
--                             where every regional row shares one capture
--                             instant. Split from the value above because 365
--                             "unresolvable ties" is a true sentence that reads
--                             as a far worse fact than the one it describes.
--
-- Nullable, and NULL on every non-primary row: it is a fact about a choice, and
-- 1,983 of the 2,828 rows are not the chosen one. `_validate` refuses to publish
-- a primary row that carries no basis.

ALTER TABLE device_current_firmware ADD COLUMN device_primary_basis TEXT
  CHECK (device_primary_basis IS NULL OR device_primary_basis IN (
    'sole_candidate',
    'latest_basis',
    'latest_stated_date',
    'publisher_currency_rank',
    'publisher_identity',
    'android_version',
    'observation_order',
    'one_publishers_region_choice',
    'arbitrary_stable_order'));

-- Staging carries the same constraints so a malformed build fails before
-- anything is published -- 0019's own words, and the reason this is two
-- statements and not one.
ALTER TABLE device_current_firmware_staging ADD COLUMN device_primary_basis TEXT;

INSERT INTO schema_migrations(version,name,applied_at)
VALUES(35,'device_primary_basis',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
COMMIT;
