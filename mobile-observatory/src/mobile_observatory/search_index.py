"""An FTS5 index that NARROWS the release search; LIKE still decides it.

`/api/v1/search` asks three questions and one of them costs almost all of it.
Measured on the live corpus, warm, medians of 5, per part:

    query        devices    chips   releases   total
    q=_             0.7ms   11.5ms     50.4ms  62.1ms
    q=5G            0.7ms   11.9ms     59.3ms  70.2ms
    q=Galaxy        0.6ms   12.2ms     71.2ms  80.2ms

So this module indexes releases and leaves the other two alone -- see
"WHAT IS DELIBERATELY NOT INDEXED" at the bottom for the measurements that made
those not worth doing.

THE SEMANTIC PROBLEM, AND WHY THIS IS NOT A PRODUCT CHANGE
----------------------------------------------------------
LIKE '%x%' matches mid-token. FTS5 is token oriented, so swapping one for the
other would change WHICH devices a reader finds -- a faster search that answers
differently is a product decision, not an optimisation, and it is not one this
module makes. Two things keep the answer identical:

1. The tokenizer is **trigram**, which is the FTS5 tokenizer built for substring
   matching rather than words.
2. The index only ever narrows. Every query still carries the same
   `database.like_clause` predicates it carried before, evaluated over the rows
   the index proposed. A trigram phrase match is a SUPERSET of the substring
   match (if the text contains the substring it contains all of that substring's
   trigrams, in order), and the LIKE re-verification removes anything extra.
   Results are therefore identical BY CONSTRUCTION, not by having been spot
   checked -- and tests/test_search_index_agrees_with_scan.py asserts it over
   58 queries including every one from the wildcard-escaping round.

WHERE IT DOES NOT HELP, STATED PLAINLY
--------------------------------------
- **1 and 2 character queries get nothing.** A trigram index cannot answer a
  query shorter than a trigram. `MIN_MATCH` sends those to the unchanged scan.
  This is not a corner: `5G`, `i3`, `_` and `%` -- four of the five queries the
  wildcard round measured -- are all shorter than three characters. Worse, FTS5
  does not raise on a too-short trigram query, it returns NO ROWS, so the gate
  is load bearing: without it the index answers "nothing matches" for `5G` and
  is believed. tests/test_search_index_agrees_with_scan.py covers exactly this.
- **Very broad queries are slower through an index than through a scan**, so
  they are not sent through it. `Galaxy` matches all 21,186 indexed rows;
  proposing 21,186 candidates and re-verifying them cost 94ms against the
  scan's 44ms. `CANDIDATE_CAP` abandons the index at that point and scans.
  Measured over 20 queries: no cap at all was 465ms, cap=8000 was 338ms, the
  plain scan 744ms.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

INDEX = "firmware_release_search"
STATE = "search_index_state"

# The columns `_search_releases` matches on, in the order the body concatenates
# them. Kept here rather than in server.py so the index and the query that reads
# it cannot come to disagree about what "searchable" means.
SEARCHABLE = ("build_id", "model_code", "variant", "target_code")

# A trigram is three characters. Shorter than that the index cannot answer, and
# it does not say so -- it returns an empty result.
MIN_MATCH = 3

# Above this many candidate rows the index costs more than it saves. Measured;
# the sweep is in the module docstring. Flat between 2,000 and 16,000, so this
# is not a knife edge.
CANDIDATE_CAP = 8000

_BODY = ("coalesce(build_id,'')||char(10)||coalesce(model_code,'')||char(10)||"
         "coalesce(variant,'')||char(10)||coalesce(target_code,'')")

# Built FROM the view the search reads, not from the underlying tables. The
# searchable columns come from three tables joined two different ways, and an
# index assembled by re-deriving that join is an index that can disagree with
# the query it serves. Reading the view makes agreement structural.
SOURCE_SQL = f"SELECT firmware_release_id, {_BODY} AS body FROM v_device_region_history"


@dataclass(frozen=True)
class Narrowing:
    """How one query should be answered, and by which path."""

    clause: str        # SQL to AND into the WHERE, or "" for none
    params: list       # its parameters, in order, before the caller's own
    path: str          # 'scan' | 'index' | 'empty'
    reason: str = ""

    @property
    def certainly_empty(self) -> bool:
        """The index proved no row can match, so the caller need not query.

        Sound only because the index is a superset of the LIKE match: nothing
        the scan would have found can be missing from the candidates.
        """
        return self.path == "empty"


def match_expression(q: str) -> str:
    """`q` as an FTS5 phrase, so no character in it is read as query syntax.

    A bare user string in a MATCH is an expression: `AND`, `*`, `-`, `"` and `(`
    all mean something. Wrapping it in double quotes makes it a phrase and
    doubling any quote inside keeps it one. This is the same class of bug as the
    unescaped LIKE that reported 865 devices for `?q=_`: a value from the request
    being read as a pattern.
    """
    return '"' + q.replace('"', '""') + '"'


# --------------------------------------------------------------------------
# building it
# --------------------------------------------------------------------------


def basis_digest(connection) -> tuple[int, str]:
    """(rows, digest) of exactly the text the index is built from.

    The digest is the whole basis, not a sample and not a row count: a rename
    that leaves the count alone is precisely the change a count cannot see.
    """
    digest = hashlib.sha256()
    rows = 0
    for release_id, body in connection.execute(SOURCE_SQL + " ORDER BY firmware_release_id"):
        digest.update(release_id.encode())
        digest.update(b"\0")
        digest.update((body or "").encode())
        digest.update(b"\0")
        rows += 1
    return rows, digest.hexdigest()


def release_count(connection) -> int:
    """The cheap tripwire the request path can afford. 0.01ms measured."""
    return connection.execute("SELECT count(*) FROM firmware_releases").fetchone()[0]


def state(connection) -> dict | None:
    row = connection.execute(
        f"SELECT name, source_rows, release_rows, basis_digest, built_at "
        f"FROM {STATE} WHERE name=?", (INDEX,)).fetchone()
    if row is None:
        return None
    return {"name": row[0], "source_rows": row[1], "release_rows": row[2],
            "basis_digest": row[3], "built_at": row[4]}


def build(connection, *, force: bool = False) -> dict:
    """Rebuild the index if its basis moved, and record what it was built from.

    Skips the rebuild when the basis digest is unchanged, which is not only
    faster: FTS5's shadow tables all have primary keys, so they are tracked by
    the session recorder, and rewriting 5.6 MB of index every night would put
    5.6 MB into every batch's changeset for no change at all.
    """
    rows, digest = basis_digest(connection)
    releases = release_count(connection)
    current = state(connection)
    if not force and current and current["basis_digest"] == digest:
        return {"rebuilt": False, "reason": "basis unchanged", "rows": rows,
                "basis_digest": digest, "built_at": current["built_at"]}
    with connection:
        connection.execute(f"DELETE FROM {INDEX}")
        connection.execute(f"INSERT INTO {INDEX}(release_id, body) {SOURCE_SQL}")
        # Merges the b-tree segments the inserts left behind. Without it the
        # index answers from many segments and every query pays for the extra
        # seeks; it is the difference between an index and a pile of them.
        connection.execute(f"INSERT INTO {INDEX}({INDEX}) VALUES('optimize')")
        connection.execute(
            f"INSERT INTO {STATE}(name, source_rows, release_rows, basis_digest, built_at) "
            f"VALUES(?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            f"ON CONFLICT(name) DO UPDATE SET source_rows=excluded.source_rows, "
            f"release_rows=excluded.release_rows, basis_digest=excluded.basis_digest, "
            f"built_at=excluded.built_at",
            (INDEX, rows, releases, digest))
    return {"rebuilt": True, "reason": "basis changed" if current else "first build",
            "rows": rows, "basis_digest": digest,
            "built_at": (state(connection) or {}).get("built_at")}


def staleness(connection) -> tuple[bool, str]:
    """Whether the index still describes the corpus. Recomputes the basis.

    ~66ms on the live corpus, which is why check_corpus calls it and the request
    path does not. The request path uses the row-count tripwire in `plan()`.
    """
    current = state(connection)
    if current is None:
        rows, _ = basis_digest(connection)
        if rows == 0:
            # Nothing to index, so nothing can be out of date. A freshly migrated
            # corpus has the table and no contents; calling that "stale" would
            # make every new corpus report an error for a defect it cannot have,
            # and an invariant that fires on a clean install is one people learn
            # to ignore.
            return False, ""
        return True, f"no index has been built, and {rows} rows are waiting to be indexed"
    rows, digest = basis_digest(connection)
    if current["basis_digest"] != digest:
        return True, (f"the index was built from {current['source_rows']} rows whose digest was "
                      f"{current['basis_digest'][:12]}; the corpus now has {rows} rows digesting "
                      f"to {digest[:12]}")
    indexed = connection.execute(f"SELECT count(*) FROM {INDEX}").fetchone()[0]
    if indexed != rows:
        return True, f"the index holds {indexed} rows for a basis of {rows}"
    return False, ""


# --------------------------------------------------------------------------
# reading it
# --------------------------------------------------------------------------

SCAN = Narrowing("", [], "scan", "")


def plan(connection, q: str, id_column: str) -> Narrowing:
    """Decide how to answer `q`, falling back to the plain scan whenever unsure.

    Every uncertainty resolves to `scan`, which is what the code did before this
    module existed: too short for a trigram, too broad to be worth narrowing, no
    index built, an index whose row count no longer matches the corpus, or any
    SQLite error out of FTS5 at all. The index is an accelerator and never an
    authority -- so the worst outcome available here is the old speed, never a
    wrong answer.
    """
    if len(q) < MIN_MATCH:
        return Narrowing("", [], "scan", f"shorter than {MIN_MATCH} characters")
    current = state(connection)
    if current is None:
        return Narrowing("", [], "scan", "no index has been built")
    # The tripwire. It catches the common staleness -- rows ingested since the
    # index was built -- for 0.01ms. It does NOT catch a same-count edit; that is
    # `staleness()`'s job, which check_corpus runs on every batch. Said plainly
    # because a guard that catches one of two cases must not be described as
    # catching both.
    if current["release_rows"] != release_count(connection):
        return Narrowing("", [], "scan", "the index predates the current releases")
    expression = match_expression(q)
    try:
        candidates = connection.execute(
            f"SELECT count(*) FROM (SELECT rowid FROM {INDEX} WHERE {INDEX} MATCH ? LIMIT ?)",
            (expression, CANDIDATE_CAP + 1)).fetchone()[0]
    except Exception as error:  # FTS5 refused the expression; the scan still works
        return Narrowing("", [], "scan", f"fts5 refused the query: {error}")
    if candidates == 0:
        return Narrowing("", [], "empty", "no candidate rows")
    if candidates > CANDIDATE_CAP:
        return Narrowing("", [], "scan", f"more than {CANDIDATE_CAP} candidate rows")
    return Narrowing(
        f"{id_column} IN (SELECT release_id FROM {INDEX} WHERE {INDEX} MATCH ?) AND ",
        [expression], "index", f"{candidates} candidate rows")


# --------------------------------------------------------------------------
# WHAT IS DELIBERATELY NOT INDEXED
# --------------------------------------------------------------------------
#
# Devices. `_search_devices` reads `device_catalog_flat`, which is 865 rows. The
# scan costs 0.7ms with a query and 0.7ms with none, so the LIKE is not the cost
# and there is nothing for an index to remove. An FTS5 table over it would add
# disk, a rebuild step and a staleness mode to save at most 0.7ms.
#
# Chips. `chips_page` costs 11.6ms with NO query and 11.7ms with `q=5G`: the
# whole of it is the CTE that counts devices and advisories per part, and the
# LIKE is free. Indexing the text would save ~0.1ms of 11.7ms. It is also the
# hardest of the three to index honestly, because one of its searchable columns
# (`product_names`) is a group_concat over a join rather than a stored value, so
# the index would be a second derivation of it that could drift.
#
# Both are the same finding: `q` was never the cost outside releases. Measuring
# first is what kept two more indexes, two more rebuilds and two more ways to be
# silently stale out of this corpus.
