"""/api/v1/product-releases sorts and pages `product_firmware_releases` alone and
joins the payload tables to the surviving page. That is row-equivalent ONLY while
every payload join is lossless -- matching exactly one row per
product_firmware_releases row, never zero and never two.

Written BEFORE the rewrite, and proven against a planted defect in both
directions, because the rewrite is indistinguishable from a correct one on
today's data and would silently change what the endpoint MEANS on data where a
join stops being lossless:

* a join that can DUPLICATE turns a page of 100 releases into 100 rows carrying
  fewer than 100 distinct releases, and the total stops counting releases;
* a join that can DROP makes the narrow count larger than the wide one, so the
  pager offers pages that render short.

The shipped fixture holds ZERO product_firmware_releases rows, so every assertion
here would pass without examining anything. This file builds its own rows and
asserts they exist first -- the same blindness that let the page-one search ship
is the reason test_query_cost.py carries
test_the_fixture_is_larger_than_the_page.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.server import (  # noqa: E402
    PRODUCT_RELEASE_JOIN_ORDER, PRODUCT_RELEASE_PAYLOAD_JOINS, ObservatoryService,
    product_release_joins)

LIVE_CORPUS = ROOT / ".observatory-data" / "corpus.sqlite"

# Enough releases that a page boundary exists and a duplicating join changes the
# page, not merely the total.
RELEASES = 60
SORTS = ("released_desc", "released_asc", "product_asc", "android_desc")


def _build(db) -> None:
    """A minimal but REAL product-evidence graph: every table the join set names."""
    c = db.connection
    c.execute("INSERT INTO sources(id,name,authority_scope,enabled,created_at,currency_rank)"
              " VALUES('src.a','Source A','secondary',1,'2026-01-01T00:00:00Z',50)")
    c.execute("INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,"
              "parser_version) VALUES('run.a','src.a','2026-01-01T00:00:00Z','succeeded','p','1')")
    c.execute("INSERT INTO artifacts(id,source_id,run_id,sha256,media_type,source_url,"
              "retrieved_at,storage_uri,byte_length) VALUES('art.a','src.a','run.a',?,"
              "'text/plain','https://example.invalid/a','2026-01-01T00:00:00Z','file:///a',1)",
              ("a" * 64,))
    for n in range(RELEASES):
        product = f"prod.{n:03d}"
        c.execute("INSERT INTO source_products(id,manufacturer,canonical_name,normalized_name,"
                  "review_state,specification_json,created_at,updated_at) VALUES(?,?,?,?,"
                  "'approved',?, '2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
                  (product, "Maker" if n % 2 else "Other", f"Device {n:03d}",
                   f"device {n:03d}", '{"slug":"spec-%03d"}' % n))
        c.execute("INSERT INTO source_identity_registry(id,source_id,namespace,source_value,"
                  "normalized_value,product_id,resolution_state,resolution_method,rule_version,"
                  "confidence,first_seen_at,last_seen_at) VALUES(?,'src.a','ns',?,?,?,"
                  "'approved','manual','1','high','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
                  (f"ident.{n:03d}", f"ident-{n:03d}", f"ident-{n:03d}", product))
        c.execute("INSERT INTO observations(id,source_id,run_id,artifact_id,record_type,"
                  "source_key,observed_at,payload_json,content_sha256,validation_state) VALUES"
                  "(?,'src.a','run.a','art.a','firmware',?, '2026-01-01T00:00:00Z',?,?,'promoted')",
                  (f"obs.{n:03d}", f"key-{n:03d}",
                   '{"data":{"security_patch_level":"2026-01-01","release_scope":"s",'
                   '"comments":"c","download_url":"https://example.invalid/d"}}',
                   f"{n:064d}"))
        c.execute("INSERT INTO observed_product_silicon(product_id,raw_chipset,vendor,part_number,"
                  "marketing_name,evidence_json,confidence,observed_at) VALUES(?,'chip','V','P',"
                  "'M','[{\"slug\":\"ops-slug\"}]','high','2026-01-01T00:00:00Z')", (product,))
        # A third of them carry no vendor release date, so the "NULLs last" half of
        # the ORDER BY is exercised rather than sorted over a uniform column.
        released = None if n % 3 == 0 else f"2026-{(n % 12) + 1:02d}-01"
        c.execute("INSERT INTO product_firmware_releases(id,product_id,identity_id,observation_id,"
                  "source_id,region_code,build_id,channel,android_version,android_major,"
                  "vendor_released_at,delivery_method,created_at) VALUES(?,?,?,?,'src.a',?,?,"
                  "'Stable',?,?,?,'OTA','2026-01-01T00:00:00Z')",
                  (f"rel.{n:03d}", product, f"ident.{n:03d}", f"obs.{n:03d}",
                   "EU" if n % 2 else "US", f"BUILD{n:03d}", str(10 + n % 7), 10 + n % 7,
                   released))


class ProductReleaseJoinsAreLosslessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database.migrated()
        self.addCleanup(self.db.close)
        _build(self.db)
        self.service = ObservatoryService(
            self.db, Path(self.temp.name) / "local.sqlite", demonstration=True)
        self.addCleanup(self.service.local.close)

    def _counts(self, connection) -> tuple[int, int, int]:
        base = connection.execute(
            "SELECT count(*) FROM product_firmware_releases").fetchone()[0]
        joins = product_release_joins(PRODUCT_RELEASE_JOIN_ORDER)
        joined = connection.execute(f"SELECT count(*) {joins}").fetchone()[0]
        distinct = connection.execute(f"SELECT count(DISTINCT pfr.id) {joins}").fetchone()[0]
        return base, joined, distinct

    def test_the_fixture_actually_holds_releases(self) -> None:
        """Without this every assertion below passes over zero rows.

        The shipped demonstration fixture has no product_firmware_releases at all.
        """
        base, joined, _ = self._counts(self.db.connection)
        self.assertEqual(RELEASES, base)
        self.assertGreater(joined, 0, "join set produced no rows; the test proves nothing")

    def test_the_payload_joins_neither_drop_nor_duplicate_a_row(self) -> None:
        base, joined, distinct = self._counts(self.db.connection)
        self.assertEqual(base, joined,
                         f"the product-releases join set is not lossless: "
                         f"{base} product_firmware_releases rows became {joined}. "
                         f"Sorting and paging pfr alone is no longer row-equivalent.")
        self.assertEqual(base, distinct,
                         f"the join set duplicates releases: {joined} rows carry only "
                         f"{distinct} distinct product_firmware_releases ids")

    def test_every_payload_join_is_on_a_unique_key(self) -> None:
        """Losslessness is structural, not a property of today's rows.

        A join on a non-unique column can duplicate however clean the data is, so
        it must be caught when it is WRITTEN, not when some corpus happens to
        exercise it.
        """
        c = self.db.connection
        offenders = []
        for alias, (sql, _deps) in PRODUCT_RELEASE_PAYLOAD_JOINS.items():
            words = sql.split()
            table = words[words.index("JOIN") + 1]
            # `<target>.<column>=<pfr or other>.<column>`: the join is lossless when
            # the TARGET side is unique, so read the column from whichever side
            # carries the target's alias.
            left, right = sql.rsplit(" ON ", 1)[1].split("=")
            target_alias = words[words.index("JOIN") + 2]
            column = (left if left.strip().startswith(target_alias + ".") else right
                      ).strip().split(".")[1]
            info = {r[1]: r for r in c.execute(f"PRAGMA table_info({table})")}
            is_pk = column in info and info[column][5] > 0
            unique = any(
                r[2] == 1 and [x[2] for x in c.execute(f"PRAGMA index_info({r[1]})")] == [column]
                for r in c.execute(f"PRAGMA index_list({table})"))
            if not (is_pk or unique):
                offenders.append(f"{alias}: {table}.{column} is neither a primary key "
                                 f"nor uniquely indexed, so this join can duplicate rows")
        self.assertEqual([], offenders, "\n  ".join([""] + offenders))

    def test_the_page_is_what_the_wide_join_would_have_returned(self) -> None:
        """A differential check against the pre-rewrite shape, over every sort and
        filter the endpoint accepts, so "faster" cannot become "different"."""
        joins = product_release_joins(PRODUCT_RELEASE_JOIN_ORDER)
        orders = {
            "released_desc": "NULLIF(pfr.vendor_released_at,'null') IS NULL,"
                             "NULLIF(pfr.vendor_released_at,'null') DESC",
            "released_asc": "NULLIF(pfr.vendor_released_at,'null') IS NULL,"
                            "NULLIF(pfr.vendor_released_at,'null') ASC",
            "product_asc": "sp.canonical_name COLLATE NOCASE ASC",
            "android_desc": "CAST(pfr.android_version AS INTEGER) DESC",
        }
        shapes = [{}, {"maker": ["Maker"]}, {"region": ["EU"]}, {"channel": ["Stable"]},
                  {"product": ["prod.007"]}, {"q": ["Device 01"]}, {"q": ["ident-02"]},
                  {"limit": ["10"], "offset": ["20"]}, {"limit": ["7"], "offset": ["55"]}]
        for sort in SORTS:
            for shape in shapes:
                query = {**shape, "sort": [sort]}
                with self.subTest(sort=sort, shape=shape):
                    page = self.service.product_releases_page(query)
                    limit = int(query.get("limit", ["100"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                    clauses, params = ["1=1"], []
                    for key, expression in (("maker", "sp.manufacturer"),
                                            ("region", "pfr.region_code"),
                                            ("channel", "pfr.channel"), ("product", "sp.id")):
                        if query.get(key):
                            clauses.append(f"{expression}=? COLLATE NOCASE")
                            params.append(query[key][0])
                    if query.get("q"):
                        from mobile_observatory.database import like_clause, like_contains
                        clauses.append("(" + " OR ".join(like_clause(col) for col in (
                            "sp.canonical_name", "pfr.build_id", "sir.source_value")) + ")")
                        params.extend([like_contains(query["q"][0])] * 3)
                    where = " AND ".join(clauses)
                    reference = self.db.connection.execute(
                        f"SELECT pfr.id {joins} WHERE {where} "
                        f"ORDER BY {orders[sort]},pfr.id DESC LIMIT ? OFFSET ?",
                        [*params, limit, offset]).fetchall()
                    total = self.db.connection.execute(
                        f"SELECT count(*) {joins} WHERE {where}", params).fetchone()[0]
                    self.assertEqual([r[0] for r in reference], [i["id"] for i in page.items])
                    self.assertEqual(total, page.total)


class TheLiveCorpusJoinsAreLosslessTest(unittest.TestCase):
    """The invariant on the real thing, skipped when the 255MB corpus is absent."""

    def setUp(self) -> None:
        if not LIVE_CORPUS.is_file():
            self.skipTest(f"no live corpus at {LIVE_CORPUS}")
        self.db = Database(LIVE_CORPUS)
        self.addCleanup(self.db.close)

    def test_the_corpus_joins_neither_drop_nor_duplicate(self) -> None:
        c = self.db.connection
        joins = product_release_joins(PRODUCT_RELEASE_JOIN_ORDER)
        base = c.execute("SELECT count(*) FROM product_firmware_releases").fetchone()[0]
        self.assertGreater(base, 0, "corpus holds no product firmware releases")
        self.assertEqual(base, c.execute(f"SELECT count(*) {joins}").fetchone()[0])
        self.assertEqual(base, c.execute(f"SELECT count(DISTINCT pfr.id) {joins}").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
