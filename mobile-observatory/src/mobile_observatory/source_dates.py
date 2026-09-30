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
