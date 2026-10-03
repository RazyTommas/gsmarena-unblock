"""One rule for reading a date a source stated.

A captured payload is the archive and is never rewritten, so a source that writes
the four-character string `"null"` where a date belongs stays that way in
`observations.payload_json` forever. What must not happen is that string reaching a
reader as a date -- and it did:

    SELECT json_extract(payload_json,'$.data.release_date') FROM observations
     WHERE id='96ea8abc-5162-5634-a86a-7657dbfe6ae9'   ->  'null'  (typeof text)

`json_extract` is faithful: the source sent a string, it returns a string.
`coalesce` then treats it as PRESENT, so `observations.effective_at` never fell
through to `observed_at`, and the word `null` was published as the effective date
of six Xiaomi builds. It propagated into
`product_firmware_releases.vendor_released_at`, where `releases_page` printed it
unguarded (server.py, the `pfr.vendor_released_at released` column).

THE RULE IS A SHAPE TEST, NOT A SENTINEL LIST. `NOT IN ('null','N/A','unknown',…)`
would be a list of guesses that grows every time a new source invents a new way to
say nothing, and every entry after the first would be unmeasured speculation. A
date either looks like a date or it is not one. Measured over all 96,319
observations, every value any source states in these three fields is either JSON
null or ISO-date-prefixed:

    $.data.release_date   87,089 JSON null · 9,224 ISO-prefixed · 6 the string 'null'
    $.data.publish_date   79,719 JSON null · 16,600 ISO-prefixed · 0 other
    $.data.release_time   96,319 JSON null · 0 other

So the shape test discards exactly the 6 rows that state a non-date and nothing
else. It is not a list that needs extending; a seventh way of writing absence is
already covered.

WHY ONE RULE AND NOT A GUARD PER READER. The sentinel had already been noticed
twice and patched locally both times -- `nullif(pfr.vendor_released_at,'null')` in
current_firmware.EVIDENCE_SQL and four more copies in server.py -- while
`observations.effective_at`, its actual origin, and `releases_page`'s own released
column were left surfacing it. That is this codebase's recorded lesson #2 ("the
same rule in two places is how the second one survived") playing out over three
months. So the rule lives here, is applied where a derived value is PRODUCED, and
the readers carry no guard at all:

  * `observations.effective_at` -- migration 0031 wraps each stated field in
    `stated_date_sql`, so the generated column reads a non-date as absence and
    falls through to `observed_at` exactly as the 87,089 JSON-null rows already do.
  * `product_firmware_releases.vendor_released_at` and
    `product_security_publications.published_at` -- written through
    `stated_date()` in enrichment.promote_approved_product_observations.
  * `integrity.check_corpus` reports the source defect that remains in the
    archive, and reports a derived column that ever holds a non-date again as an
    ERROR, because that can only mean a write bypassed this module.

`stated_date` and `stated_date_sql` must agree, and
tests/test_source_stated_dates.py asserts that by running both over the same
values rather than by inspection.

WHICH FIELD, NOT JUST WHICH SHAPE. 2026-10-04.

A date that looks like a date is only half the rule. The other half is the NAME
the source wrote it under, and that half was broken in exactly the shape this
codebase keeps paying for: `promote_approved_product_observations` read
`$.data.release_date` and `$.data.branch`, which is the xiaomi tracker's
vocabulary, while `mifirm.community.firmware_archive` publishes the same two
facts as `$.data.vendor_released_at` and `$.data.channel`. Nothing errored. All
21,845 promoted mifirm releases simply landed with `vendor_released_at IS NULL`
and `channel='unknown'`, and because `android_version_changed` orders its
before/after pair by `vendor_released_at, id`, 5,643 of 5,804 published
"this build came after that one" claims were decided by a uuid5 row id.

So the accepted SPELLINGS are declared here too, beside the shape test, with the
fields that are deliberately NOT read listed next to the reason. Measured across
all 96,319 captured observations, the complete inventory of date-ish and
channel-ish keys any adapter publishes is:

    $.data.release_date        xiaomi.community.firmware_tracker, ipsw.me   READ
    $.data.vendor_released_at mifirm.community.firmware_archive   44,351    READ
    $.data.publish_date       samsung.doc.aspl, tecno.vendor.security       READ (bulletins)
    $.data.release_time       samsung.fota (all NULL)                       READ
    $.data.build_date         frbox.community.transsion_catalog    1,551    NOT read
    $.data.date_token         naijarom.community.transsion_firmware         NOT read
    $.data.build_derived_month samsung.fota                                 NOT read
    $.data.branch             xiaomi.community.firmware_tracker             READ (channel)
    $.data.channel            mifirm.…firmware_archive, samsung.fota        READ (channel)

The three NOT-read fields are not oversights and are not shape failures either:
each adapter states in its own payload what its date measures, and all three say
it is not a vendor release date --
`build_date_extracted_from_version_string_not_patch_level`,
`opaque_vendor_token_not_a_patch_level`,
`build_identifier_month_not_vendor_release`. Reading any of them into
`vendor_released_at` would put a date parsed out of a filename next to a date a
vendor published and then sort the two, which is the one thing this corpus is not
allowed to do.

tests/test_firmware_order_basis.py SCANS every adapter for a key that is date-ish
or channel-ish and names none of these lists, and fails if one appears. That is
the part that catches the next adapter rather than the last one: a declared list
nobody checks is how the first spelling drifted from the second.

ONE GAP IS KNOWN AND DELIBERATELY STILL OPEN. `STATED_DATE_FIELDS` below drives
`observations.effective_at` (migration 0031), and it does NOT list
`$.data.vendor_released_at` -- so that generated column still falls through to
`observed_at` for all 44,351 mifirm observations. It is not closed here because
`effective_at` already mixes three different measurements in one sortable column
(a vendor release date, a bulletin publication date and a capture time) and the
Explore observations tab sorts on it; adding a fourth publisher's real dates
changes that ordering for 46% of the corpus and the honest repair is the banding
decision `devices_page` already had to make, which is a judgement and not a
rename. `check_corpus` reports it as
`source_stated_release_date_not_read_by_the_generated_column` so it is loud
rather than silent.
"""
from __future__ import annotations

import re

# ISO 8601 calendar date, as a GLOB pattern. GLOB's `[0-9]` is ASCII digits only,
# which is why the Python mirror below spells the character class out rather than
# using `\d` -- `\d` matches Devanagari digits and the two would disagree on input
# no source has sent yet.
ISO_DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
_ISO_DATE_PREFIX = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}")

# The fields a source uses to state when a build or bulletin was released, in the
# precedence the read path applies. Named here so the check that reports the source
# defect and the generated column cannot disagree about which fields are dates.
STATED_DATE_FIELDS = ("$.data.release_date", "$.data.publish_date", "$.data.release_time")

# The spellings that mean "the vendor stated when this build was released", in
# precedence order, as bare `data` keys. All are read; no captured payload states
# more than one and the first wins if one ever does.
#
# `release_time` is in here because `collectors.promotion.SamsungFirmwarePromoter`
# writes it to `firmware_releases.vendor_released_at` -- the same fact under a
# third name, found by the adapter scan rather than by reading. It is non-null in
# 0 of 96,319 captured observations, so listing it changes nothing today and stops
# it being the next `vendor_released_at`.
VENDOR_RELEASE_DATE_FIELDS = ("release_date", "vendor_released_at", "release_time")

# The spellings that mean "which release channel / branch is this build on".
STATED_CHANNEL_FIELDS = ("branch", "channel")

# Date-ish keys that exist in captured payloads and are deliberately NOT read as
# a vendor release date, each with the adapter's OWN stated reason for why it is
# not one. This list is not a convenience: it is what makes the adapter scan in
# tests/test_firmware_order_basis.py able to tell "a spelling nobody wired up"
# from "a value we decided not to read", which is the distinction the mifirm
# defect destroyed.
DATE_FIELDS_NOT_A_RELEASE_DATE = {
    "build_date": "build_date_extracted_from_version_string_not_patch_level",
    "date_token": "opaque_vendor_token_not_a_patch_level",
    "build_derived_month": "build_identifier_month_not_vendor_release",
}

# Date-ish keys read as a BULLETIN publication date rather than a build's release
# date. product_security_publications.published_at, not vendor_released_at.
PUBLICATION_DATE_FIELDS = ("publish_date",)

# Date-ish keys that are a SECURITY PATCH TIER and not a date anything happened
# on. `aspl_month` is month-precision on purpose (validation.py refuses invented
# day precision) and `aspl_date` exists only for the sources that publish a day.
# Reading either as a release date would state that a build shipped on the day its
# patch level is named after.
SECURITY_PATCH_LEVEL_FIELDS = ("aspl_month", "aspl_date")


def vendor_release_date(data: dict) -> str | None:
    """The date the source stated this build was released, or None.

    One function so the field NAMES live in one place. Both halves of the rule
    apply: the key must be one the sources actually use for this fact, and the
    value must pass the shape test above. A source that states a non-date under a
    name we do read is absence, exactly as before.
    """
    for field in VENDOR_RELEASE_DATE_FIELDS:
        value = stated_date(data.get(field))
        if value is not None:
            return value
    return None


def stated_channel(data: dict, absent: str = "unknown") -> str:
    """The release channel the source named, or `absent` when it named none.

    `absent` is a value and not NULL because `product_firmware_releases.channel`
    is NOT NULL and part of its UNIQUE key -- see migration 0007. 3,190 promoted
    releases are honestly channel-less (naijarom, google.ota.checkin and frbox
    publish no channel at all) and they keep saying so; what must not happen is a
    source that DID name one being recorded the same way.
    """
    for field in STATED_CHANNEL_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return absent


def stated_date_sql(expression: str) -> str:
    """SQL for `expression` when it states a date, and NULL when it does not.

    A prefix match, not an anchored one: `publish_date` legitimately carries
    `2021-08-17 03:53:18`, so requiring the value to BE ten characters would throw
    away 16,600 real timestamps. What is rejected is a value whose first ten
    characters are not a date.
    """
    return f"CASE WHEN ({expression}) GLOB '{ISO_DATE_GLOB}*' THEN ({expression}) END"


def stated_date(value: object) -> str | None:
    """The same rule in Python: the value when it states a date, else None.

    Deliberately does NOT strip whitespace, so it answers exactly what
    `stated_date_sql` answers for the same input. A value that needs trimming to
    look like a date is a source defect to report, not one to quietly repair.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        # A number or a bool in a date field is a non-date, and str()-ing it first
        # would let `20240101` through as if the source had stated something.
        return None
    return value if _ISO_DATE_PREFIX.match(value) else None
