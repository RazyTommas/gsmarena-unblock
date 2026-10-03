from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import sqlite3
import sys
import time
from datetime import datetime, timezone
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from . import search_index
from .access import COOKIE_NAME, AccessPolicy, token_for_binding
from .posture import ExposureMonitor, Posture, posture_at_startup, token_provenance
from .worker_lock import exclusive_worker

from .watches import migrate_watches, list_watches, save_watch
from .proposals import migrate_proposals, import_proposals, list_proposals, review_proposal, save_decision
from .database import Database, like_clause, like_contains
from .seed import DEMO_TIME, seed_demonstration
from .current_firmware import (ProjectionError, build as build_current_firmware,
                               state as current_firmware_state)
from .identity_bridge import refresh_observation_link_states
from .integrity import check_corpus, review_queue, summarise
from .silence import STATUS_SILENT, detect_silence
from .collection_worker import CollectionWorker, WorkerPaths, migrate_collection_queue

REGION_OPTIONS = {"ILO": "Israel (Samsung CSC)", "MID": "Middle East group", "XSG": "United Arab Emirates / Gulf", "EUX": "Europe multi-CSC", "GLOBAL": "Global", "EEA": "European Economic Area"}
SOURCE_OPTIONS = {"samsung": "Samsung FOTA/OTA", "xiaomi": "Xiaomi firmware tracker", "tecno": "Tecno security", "gsmarena": "GSMArena specifications", "android": "Android bulletins", "qualcomm": "Qualcomm advisories", "mediatek": "MediaTek advisories", "apple": "Apple firmware/security"}


def _silence_fields(finding: dict | None) -> dict:
    """Advisory silence labels merged onto a health() row. `silenceAdvisory`
    is always True: this never blocks or alters anything by itself; it is a
    label for a consumer (the admin health page, source-warnings count, or a
    scheduled batch's own alarm) to act on. See docs/SOURCE_SILENCE_DETECTION.md."""
    if finding is None:
        return {"silenceStatus": "not_applicable", "silent": False,
                "expectedIntervalHours": None, "overdueHours": None, "silenceAdvisory": True}
    return {"silenceStatus": finding["status"], "silent": finding["status"] == STATUS_SILENT,
            "expectedIntervalHours": finding["expected_interval_hours"],
            "overdueHours": finding["overdue_hours"], "silenceAdvisory": True}


# --- the product-releases join set, in one place -------------------------------
# Named because product_releases_page no longer runs all of it for every step, and
# the split is only row-equivalent while these joins are LOSSLESS: every one of
# them must match exactly one row for each product_firmware_releases row, never
# zero and never two.
#
# That holds structurally today -- each join is on the target's PRIMARY KEY
# (source_products.id, source_identity_registry.id, observations.id, artifacts.id,
# observed_product_silicon.product_id), and each referencing column is NOT NULL
# with a foreign key -- and it holds on the corpus: 27,841 rows in, 27,841 out.
# Neither fact is assumed. tests/test_product_release_joins_are_lossless.py
# asserts it against real rows and fails if a join is added that can duplicate or
# an orphan makes one drop.
PRODUCT_RELEASE_SELECTION_JOINS = {
    # alias -> (join SQL, aliases it needs joined first)
    "sp": ("JOIN source_products sp ON sp.id=pfr.product_id", ()),
    "sir": ("JOIN source_identity_registry sir ON sir.id=pfr.identity_id", ()),
}
PRODUCT_RELEASE_PAYLOAD_JOINS = {
    "o": ("JOIN observations o ON o.id=pfr.observation_id", ()),
    "ar": ("JOIN artifacts ar ON ar.id=o.artifact_id", ("o",)),
    "ops": ("LEFT JOIN observed_product_silicon ops ON ops.product_id=sp.id", ("sp",)),
}
PRODUCT_RELEASE_JOIN_ORDER = ("sp", "sir", "o", "ar", "ops")
PRODUCT_RELEASE_ALL_JOINS = {**PRODUCT_RELEASE_SELECTION_JOINS, **PRODUCT_RELEASE_PAYLOAD_JOINS}


def product_release_joins(aliases) -> str:
    """The FROM clause joining only `aliases`, plus whatever those depend on.

    Emitted in a fixed order so the SQL text for a given alias set is stable and
    a plan test can be written against it.
    """
    wanted = set(aliases)
    for alias in list(wanted):
        wanted.update(PRODUCT_RELEASE_ALL_JOINS[alias][1])
    # A dependency pulled in by the loop above may itself depend on something.
    changed = True
    while changed:
        changed = False
        for alias in list(wanted):
            for need in PRODUCT_RELEASE_ALL_JOINS[alias][1]:
                if need not in wanted:
                    wanted.add(need)
                    changed = True
    return "FROM product_firmware_releases pfr" + "".join(
        "\n          " + PRODUCT_RELEASE_ALL_JOINS[alias][0]
        for alias in PRODUCT_RELEASE_JOIN_ORDER if alias in wanted)


@dataclass(frozen=True)
class QueryPage:
    items: list[dict]
    total: int
    limit: int
    offset: int

    def metadata(self) -> dict:
        following = self.offset + len(self.items)
        return {"returned": len(self.items), "total": self.total, "limit": self.limit,
                "offset": self.offset,
                "nextCursor": str(following) if following < self.total else None}


class ObservatoryService:
    # How long a set of invariant findings may be reused. Short enough that an
    # operator watching a batch sees it clear within one refresh.
    INTEGRITY_CACHE_SECONDS = 20

    def __init__(self, corpus: Database, local_path: str | Path, *, demonstration: bool,
                 sample_path: str | Path | None = None,
                 legacy_root: str | Path | None = None,
                 fixture_root: str | Path | None = None) -> None:
        self.corpus = corpus
        self.demonstration = demonstration
        # self.local stays a single connection guarded by self.local_lock below.
        # It is tiny, write-mostly read-state, so one locked connection is cheaper
        # than the per-thread pool the corpus needs.
        #
        # That is only true while EVERY use is actually inside the lock. This
        # comment previously asserted it was, and it was not: config(),
        # save_config(), identity_decisions(), collection_requests() and
        # request_collection() all touched the connection unguarded, which is the
        # same defect bc54d25 fixed on the corpus connection. Measured at 4a51462,
        # 16 threads x 40 ops: 14 of 16 threads died with InterfaceError,
        # TypeError from a half-read row, and a duplicate-column DDL race.
        # tests/test_service_threading.py now enforces the claim instead of
        # asserting it in prose.

        self.sample_path = Path(sample_path) if sample_path else None
        self.data_dir = Path(local_path).parent
        project_root = Path(__file__).resolve().parents[2]
        self.worker_paths = WorkerPaths(
            self.data_dir / "ledger",
            Path(legacy_root) if legacy_root else project_root.parent / "crawler" / "relay" / "results",
            Path(fixture_root) if fixture_root else project_root / "fixtures",
        )
        self._integrity_cache: tuple[float, dict] | None = None
        # Ids inserted by the most recent dismiss_unwatched(), so undo_bulk_dismissal()
        # can delete exactly that batch. Guarded by local_lock like every other
        # piece of local state.
        self._undo_batch: list[str] = []
        self.local = sqlite3.connect(str(local_path), check_same_thread=False)
        self.local_lock = threading.RLock()
        self.local.row_factory = sqlite3.Row
        self.local.execute("PRAGMA journal_mode = WAL")
        self.local.execute(
            """CREATE TABLE IF NOT EXISTS acknowledgements (
                 event_id TEXT PRIMARY KEY, acknowledged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        self.local.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
        self.local.execute("""CREATE TABLE IF NOT EXISTS collection_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, source TEXT NOT NULL,
          scope TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        migrate_collection_queue(self.local)
        migrate_watches(self.local)
        self.local.execute("""CREATE TABLE IF NOT EXISTS identity_decisions (
          source_namespace TEXT NOT NULL, source_value TEXT NOT NULL, canonical_type TEXT NOT NULL,
          canonical_id TEXT, decision TEXT NOT NULL CHECK(decision IN ('same','different','defer')),
          decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, rationale TEXT,
          PRIMARY KEY(source_namespace,source_value,canonical_type,canonical_id))""")
        self.local.commit()
        migrate_proposals(self.local)
        self._reapply_product_reviews()
        try:
            CollectionWorker(self.local, self.corpus.connection, self.worker_paths).recover_interrupted()
        except ValueError:
            pass  # Another live worker owns the lock; preserve its active jobs.

    def release_thread_connections(self) -> None:
        """Release per-thread database connections held by the calling thread.

        The request handler calls this when it is done with a connection. Kept on
        the service rather than reaching into `.corpus` from the handler so that a
        second per-thread database added later has one obvious place to be freed.
        """
        self.corpus.release_thread()

    @property
    def meta(self) -> dict:
        row = self.corpus.connection.execute(
            "SELECT max(observed_at) FROM observations"
        ).fetchone()
        data_as_of = row[0] if row and row[0] else (DEMO_TIME if self.demonstration else None)
        return {
            "snapshot": f"DEMONSTRATION · {data_as_of}" if self.demonstration else f"IMPORTED SNAPSHOT · {data_as_of or 'observation time unknown'}",
            "mode": "demonstration" if self.demonstration else "snapshot",
            "dataAsOf": data_as_of,
        }

    def _current_event_source(self) -> str:
        available = self.corpus.connection.execute("SELECT 1 FROM sqlite_schema WHERE type='view' AND name='v_current_domain_events'").fetchone()
        return "v_current_domain_events" if available else "domain_events"

    def _radar_event_source(self) -> str:
        return f"(SELECT * FROM {self._current_event_source()} WHERE event_type IN ('firmware_first_observed','firmware_replaced','android_version_changed','security_patch_changed','baseband_changed'))"

    def updates(self, query: dict[str, list[str]]) -> list[dict]:
        return self.updates_page(query).items

    def _radar_state(self) -> tuple[set[tuple[str, str]], list[str]]:
        """The watch list and the acknowledgement list, read under one lock."""
        with self.local_lock:
            watched = {(r["subject_type"], r["subject_id"]) for r in list_watches(self.local)}
            seen = [r[0] for r in self.local.execute("SELECT event_id FROM acknowledgements")]
        return watched, seen

    # The radar's predicate is built by the four helpers below and nowhere else.
    # updates_page() renders the rows they select; dismiss_unwatched() acts on
    # them. A second copy of "which events is the operator looking at" is how a
    # bulk action dismisses a row the operator could not see -- the same shape
    # as the bug that lived in five call sites.

    def _event_watch_clause(self, watched) -> tuple[str, list[object]]:
        """Is a domain event's subject on the watch list?

        `coalesce`, not the bare column: `NULL IN (...)` is NULL, so NEGATING
        this clause would drop an event whose subject resolves to neither a
        firmware release nor a source product -- an unwatched row silently
        missing from "dismiss all unwatched". `''` is never a watch id, because
        save_watch() refuses an empty one.
        """
        return ("(coalesce(fr.hardware_model_id,'') IN (SELECT value FROM json_each(?))"
                " OR coalesce(sp.id,'') IN (SELECT value FROM json_each(?)))",
                [json.dumps([i for t, i in watched if t == "hardware_model"]),
                 json.dumps([i for t, i in watched if t == "source_product"])])

    def _release_watch_clause(self, watched) -> tuple[str, list[object]]:
        """The same question against v_device_region_history, which has only the
        canonical column: a lone release carries no source_product identity."""
        return ("coalesce(hardware_model_id,'') IN (SELECT value FROM json_each(?))",
                [json.dumps([i for t, i in watched if t == "hardware_model"])])

    def _event_feed_query(self, query: dict[str, list[str]], watched, seen
                          ) -> tuple[str, str, list[object]]:
        """`joins`, `where` and params for the domain-event radar feed."""
        tab = _first(query, "tab", "history")
        change_filter = _first(query, "change")
        q = _first(query, "q").strip()
        clauses = ["1=1"]
        params: list[object] = []
        if q:
            clauses.append("(" + " OR ".join(like_clause(column) for column in (
                "coalesce(dc.brand,sp.manufacturer)",
                "coalesce(dc.variant,sp.canonical_name)",
                "dc.model_code", "de.after_json")) + ")")
            params.extend([like_contains(q)] * 4)
        for key, column in (("maker", "coalesce(dc.brand,sp.manufacturer)"),
                            ("model", "coalesce(dc.model_code,sir.source_value)"),
                            ("region", "coalesce(ft.target_code,json_extract(de.after_json,'$.region'))")):
            value = _first(query, key).strip()
            if value:
                clauses.append(like_clause(column))
                params.append(like_contains(value))
        if tab == "new":
            clauses.append("de.id NOT IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(seen))
        if tab == "watched":
            # _event_watch_clause, not an inline copy: it coalesces, and NEGATING
            # the bare `col IN (...)` form drops an event whose subject resolves to
            # neither layer -- an unwatched row surviving "dismiss all unwatched".
            # Both sides of this merge edited this block; the escaping below and
            # this shared clause are separate fixes and the merge keeps both.
            clause, watch_params = self._event_watch_clause(watched)
            clauses.append(clause)
            params.extend(watch_params)
        if change_filter == "Android upgrade":
            clauses.append("json_extract(de.before_json,'$.android') IS NOT NULL AND json_extract(de.after_json,'$.android') IS NOT NULL AND CAST(json_extract(de.before_json,'$.android') AS TEXT)!=CAST(json_extract(de.after_json,'$.android') AS TEXT)")
        elif change_filter == "Security patch":
            clauses.append("de.event_type='security_patch_changed'")
        joins = f"""FROM {self._radar_event_source()} de
                LEFT JOIN firmware_releases fr ON fr.id=de.subject_id
                LEFT JOIN v_device_catalog dc ON dc.hardware_model_id=fr.hardware_model_id
                LEFT JOIN firmware_targets ft ON ft.id=fr.firmware_target_id
                LEFT JOIN source_products sp ON de.subject_type='source_product' AND sp.id=de.subject_id
                LEFT JOIN source_identity_registry sir ON sir.product_id=sp.id"""
        return joins, " AND ".join(clauses), params

    def _release_feed_query(self, query: dict[str, list[str]], watched, seen
                            ) -> tuple[str, list[object]]:
        """`where` and params for the fallback feed used when no event exists."""
        tab = _first(query, "tab", "history")
        change_filter = _first(query, "change")
        clauses, params = _sql_filters(query, {"maker": "brand", "region": "target_code",
                                               "model": "model_code"},
                                      ("brand", "variant", "model_code", "target_code", "target_name", "build_id"))
        if tab == "new":
            clauses.append("firmware_release_id NOT IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(seen))
        if tab == "watched":
            clause, watch_params = self._release_watch_clause(watched)
            clauses.append(clause)
            params.extend(watch_params)
        if change_filter in ("Android upgrade", "Security patch"):
            clauses.append("0")  # A lone release has no previous observation proving change.
        return (" AND ".join(clauses) if clauses else "1=1"), params

    def updates_page(self, query: dict[str, list[str]]) -> QueryPage:
        watched, seen = self._radar_state()
        seen_ids = set(seen)
        event_count = self.corpus.connection.execute("SELECT count(*) FROM domain_events").fetchone()[0]
        if event_count:
            joins, where, params = self._event_feed_query(query, watched, seen)
            total = self.corpus.connection.execute(f"SELECT count(DISTINCT de.id) {joins} WHERE {where}", params).fetchone()[0]
            limit, offset = _pagination(query)
            rows = self.corpus.connection.execute(
                f"""SELECT de.id, de.event_type, de.occurred_at, de.recorded_at, de.before_json, de.after_json,
                           fr.hardware_model_id watch_hardware_id,sp.id watch_product_id,
                           coalesce(dc.brand,sp.manufacturer) maker,
                           coalesce(dc.variant,sp.canonical_name) device,
                           coalesce(dc.model_code,
                             (SELECT source_value FROM source_identity_registry esi
                              WHERE esi.id=json_extract(de.after_json,'$.source_identity')),
                             min(sir.source_value)) model,
                           coalesce(ft.display_name,ft.target_code,json_extract(de.after_json,'$.region'),'Unknown target') region
                    {joins} WHERE {where} GROUP BY de.id ORDER BY de.recorded_at DESC, de.id DESC LIMIT ? OFFSET ?""",
                [*params, limit, offset]).fetchall()
            result = []
            for row in rows:
                before = json.loads(row["before_json"]) if row["before_json"] else {}
                after = json.loads(row["after_json"]) if row["after_json"] else {}
                android_changed = (before.get("android") is not None and
                                   after.get("android") is not None and
                                   str(before["android"]) != str(after["android"]))
                change = ("Android upgrade" if android_changed else
                          "Security patch" if row["event_type"] == "security_patch_changed" else
                          row["event_type"].replace("_", " ").title())
                subject_type = "hardware_model" if row["watch_hardware_id"] else "source_product"
                subject_id = row["watch_hardware_id"] or row["watch_product_id"]
                result.append({"id": row["id"], "subjectType": subject_type, "subjectId": subject_id, "maker": row["maker"], "device": row["device"],
                    "model": row["model"], "region": row["region"], "age": row["recorded_at"],
                    "detectedAt": row["recorded_at"], "effectiveAt": row["occurred_at"],
                    "buildFrom": before.get("build", "No prior observation"), "buildTo": after.get("build", "Unknown"),
                    "androidFrom": before.get("android") or "Unknown", "androidTo": after.get("android") or "Unknown",
                    "patchFrom": before.get("security_patch") or "Unknown", "patchTo": after.get("security_patch") or "Unknown",
                    "change": change, "importance": "high" if android_changed else "medium", "watched": (subject_type,subject_id) in watched,
                    # Per row, so the client never needs the whole
                    # acknowledgement list. Shipping all of it cost 220KB on
                    # every cold load -- the single largest response in the
                    # boot -- to answer a question only about the 100 rows on
                    # screen.
                    "acknowledged": row["id"] in seen_ids})
            return QueryPage(result, total, limit, offset)
        where, params = self._release_feed_query(query, watched, seen)
        total = self.corpus.connection.execute(
            f"SELECT count(*) FROM v_device_region_history WHERE {where}", params).fetchone()[0]
        limit, offset = _pagination(query)
        rows = self.corpus.connection.execute(
            f"""SELECT firmware_release_id id, hardware_model_id,brand maker, variant device,
                       model_code model, coalesce(target_name,target_code) region,
                       build_id build_to, os_major android_to,
                       security_patch_level patch_to,first_observed_at,vendor_released_at
                FROM v_device_region_history WHERE {where}
                ORDER BY first_observed_at DESC, firmware_release_id DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset]).fetchall()
        result = [
            {
                "id": row["id"], "maker": row["maker"], "device": row["device"],
                "model": row["model"], "region": row["region"] or "Unknown target",
                "age": row["first_observed_at"], "detectedAt": row["first_observed_at"],
                "effectiveAt": row["vendor_released_at"], "buildFrom": "No prior observation",
                "buildTo": row["build_to"], "androidFrom": "Unknown",
                "androidTo": row["android_to"], "patchFrom": "Unknown",
                "patchTo": row["patch_to"], "change": "First observation",
                "importance": "medium", "watched": ("hardware_model",row["hardware_model_id"]) in watched,
                "acknowledged": row["id"] in seen_ids,
                "subjectType": "hardware_model", "subjectId": row["hardware_model_id"],
            }
            for row in rows
        ]
        return QueryPage(result, total, limit, offset)

    def _support_for(self, device_ids) -> dict[str, dict]:
        """Currently-valid support assertion for each device on THIS page.

        One query for the page rather than a correlated subquery per candidate
        row. As a join condition it ran 53,436 times to render 100 rows and
        blocked the planner from using the catalogue's name index; measured at
        176x device volume, moving it here took /devices from 228ms to 18ms.

        Evaluated against now() on every request rather than baked into the
        nightly projection, because an assertion is valid over a time window and
        a cached copy would be wrong for up to a day whenever one opens or
        expires.
        """
        ids = list(dict.fromkeys(device_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.corpus.connection.execute(
            f"""SELECT sa.subject_id, sa.status, sa.evidence_id, sa.asserted_at
                  FROM support_assertions sa
                 WHERE sa.subject_type='hardware_model'
                   AND sa.subject_id IN ({placeholders})
                   AND (sa.valid_from IS NULL OR datetime(sa.valid_from)<=datetime('now'))
                   AND (sa.valid_to IS NULL OR datetime(sa.valid_to)>datetime('now'))
                 ORDER BY sa.asserted_at, sa.id""", ids).fetchall()
        # Later rows win, matching the ORDER BY asserted_at DESC, id DESC the
        # join used to apply.
        return {row["subject_id"]: {"support_status": row["status"],
                                    "support_evidence_id": row["evidence_id"],
                                    "support_asserted_at": row["asserted_at"]}
                for row in rows}

    # The three things a reader can be told about a device's firmware. Named
    # here because two views render them and both must mean the same thing.
    COVERAGE_OBSERVED = "observed"              # a current build is established
    COVERAGE_HELD_NOT_CURRENT = "held_not_current"  # releases captured, none current
    COVERAGE_NOT_OBSERVED = "not_observed"      # nothing captured at all

    def _firmware_holdings(self, hardware_model_ids) -> dict[str, dict]:
        """How much firmware the corpus HOLDS for each device, and whether any of
        it establishes a current build.

        The one answer behind both the grid and the device detail view. They used
        to count separately and disagreed:

          /api/v1/devices?model=TECNO%20i3  ->  firmware_count 0,
                                                "Catalogued; firmware not observed"
          /api/v1/devices/TECNO%20i3        ->  "Captured ROM history · 1"

        Both numbers were honestly derived and neither was wrong on its own. The
        grid read device_current_firmware, the projection of what is CURRENT; the
        detail view read product_firmware_releases, what was CAPTURED. For TECNO
        i3 those differ because the projection deliberately refuses one build --
        `i3Pro-...` names a sibling model, see current_firmware.EVIDENCE_SQL --
        and refusing to call it current is right. Printing "firmware not
        observed" about a device whose next panel lists a build is not.

        Measured on the live corpus: 7 of 865 devices diverge, all 7 carrying one
        of the 8 sibling-model builds the `firmware_build_names_a_sibling_model`
        invariant reports. TECNO i3 is only the instance where the gap crosses
        zero and the two views contradict each other in words rather than in a
        count. So this fixes the class: `held` is what the detail view lists, and
        the grid now shows that number, with coverage saying whether any of it
        establishes a current build. The other 858 devices' numbers do not move
        -- verified before the change, not asserted.

        A device lives in exactly one layer -- the projection refuses to publish
        otherwise, and 0 of 865 hold both -- so the two counts add rather than
        needing a layer decision here.

        One query for the page, like _support_for above: three aggregates over
        the page's ids, NOT three correlated subqueries per row, and over
        firmware_releases rather than v_device_region_history.

        Both of those are measured, because the obvious way to write this was
        expensive in the way this codebase has already paid for once. On the live
        corpus, /devices at limit=500 (the largest page the UI offers):

            4.6 ms  without this lookup at all
           35.8 ms  correlated subqueries over v_device_region_history
           33.1 ms  the same counts grouped once per page
           11.2 ms  grouped, over firmware_releases

        So the correlated shape was NOT the cost, which is why it was measured
        rather than assumed. The cost is the view: v_device_region_history
        inner-joins v_device_catalog, i.e. five identity tables, and a count over
        it pays for all of them to answer a question about one column. The
        default 100-row page goes 1.1ms -> 1.8ms.

        Counting the base table is equivalent for these ids, not merely
        coincidentally: the view's one inner join is
        `dc.hardware_model_id = fr.hardware_model_id`, so it can only DROP
        releases whose hardware model is absent from the catalogue -- and every id
        here comes from the catalogue. Measured on the live corpus as well: the
        view and firmware_releases both hold exactly 21,186 rows. And it is
        checked rather than trusted --
        tests/test_device_views_agree.py compares this count against the detail
        view's own total, which still counts the view, for every device.

        The id list goes in as JSON rather than as 1,500 placeholders so the
        statement text does not grow with the page.
        """
        ids = list(dict.fromkeys(hardware_model_ids))
        if not ids:
            return {}
        rows = self.corpus.connection.execute(
            """WITH page(id) AS (SELECT value FROM json_each(?)),
                 canonical AS (SELECT hardware_model_id hm, count(*) n
                                 FROM firmware_releases
                                WHERE hardware_model_id IN (SELECT id FROM page)
                                GROUP BY hardware_model_id),
                 evidence AS (SELECT hardware_model_id hm, count(*) n
                                FROM product_firmware_releases
                               WHERE hardware_model_id IN (SELECT id FROM page)
                               GROUP BY hardware_model_id),
                 established AS (SELECT DISTINCT hardware_model_id hm
                                   FROM device_current_firmware
                                  WHERE hardware_model_id IN (SELECT id FROM page))
               SELECT p.id AS id,
                      coalesce(c.n, 0) + coalesce(e.n, 0) AS held,
                      est.hm IS NOT NULL AS current
                 FROM page p
                 LEFT JOIN canonical c ON c.hm = p.id
                 LEFT JOIN evidence e ON e.hm = p.id
                 LEFT JOIN established est ON est.hm = p.id""",
            (json.dumps(ids),)).fetchall()
        return {row["id"]: {"firmware_count": row["held"],
                            "firmwareCoverage": (
                                self.COVERAGE_OBSERVED if row["current"]
                                else self.COVERAGE_HELD_NOT_CURRENT if row["held"]
                                else self.COVERAGE_NOT_OBSERVED)}
                for row in rows}

    # What each coverage state says in place of a region list. Kept beside the
    # states themselves so a new state cannot ship without a sentence, and so
    # the grid and the detail view quote the same one.
    COVERAGE_NOTE = {
        COVERAGE_NOT_OBSERVED: "Catalogued; firmware not observed",
        COVERAGE_HELD_NOT_CURRENT: "Captured releases; none establishes current firmware",
    }

    def devices(self, query: dict[str, list[str]]) -> list[dict]:
        return self.devices_page(query).items

    def devices_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses, params = _sql_filters(query, {
            "maker": "dc.brand", "vendor": "dc.silicon_vendor", "family": "dc.silicon_family",
            "part": "dc.chip_part_number", "region": "lf.device_target_codes", "model": "dc.model_code"},
            ("dc.brand", "dc.variant", "dc.model_code", "dc.codename",
             "dc.chip_marketing_name", "dc.chip_part_number"))
        support = _first(query, "support").strip()
        support_codes = {"Supported": "officially_supported", "Likely supported": "likely_supported",
                         "End announced": "end_announced", "Unsupported": "unsupported", "Unknown": "unknown"}
        if support and support != "all":
            # Filtering by support still needs the join, so it is added only for
            # the requests that ask for it rather than paid for by every request.
            clauses.append("""coalesce((SELECT sa2.status FROM support_assertions sa2
                 WHERE sa2.subject_type='hardware_model' AND sa2.subject_id=dc.hardware_model_id
                   AND (sa2.valid_from IS NULL OR datetime(sa2.valid_from)<=datetime('now'))
                   AND (sa2.valid_to IS NULL OR datetime(sa2.valid_to)>datetime('now'))
                 ORDER BY sa2.asserted_at DESC,sa2.id DESC LIMIT 1),'unknown')=?""")
            params.append(support_codes.get(support, support))
        max_android = _first(query, "max_android").strip()
        if max_android:
            # Parse BEFORE appending the clause. It used to append first, so when
            # int() raised on `max_android=abc` the clause -- carrying a `?` --
            # was already in the list, the except branch appended a second clause
            # `"0"` with no placeholder of its own, and one `?` went unbound:
            #   sqlite3.ProgrammingError: Incorrect number of bindings supplied.
            #   The current statement uses 3, and there are 2 supplied.
            # The connection dropped with no HTTP response, so the intent the
            # comment below describes had never once executed.
            #
            # The clamp is the same one _pagination needs: `max_android=10**20`
            # reached the driver as an unbindable int and dropped the connection
            # by the other half of this defect's family. An Android major above
            # every observed value means "no ceiling", and the clamped bound says
            # exactly that.
            try:
                ceiling = int(max_android)
            except ValueError:
                # A non-numeric ceiling is not a version, so it selects nothing.
                # No placeholder, and now no orphaned one either.
                clauses.append("0")
            else:
                # Same gate as the rendered value: a device whose latest build is
                # only a capture-order guess has no established Android version,
                # so it must not satisfy a version filter either. A filter that
                # matched here would put the device in a result set defined by a
                # number the UI refuses to show for it.
                clauses.append("(lf.android_major IS NOT NULL AND lf.android_major <= ? "
                               "AND lf.latest_basis!='observation_order_only')")
                params.append(max(SQLITE_MIN_INT, min(SQLITE_MAX_INT, ceiling)))
        # device_catalog_flat, not v_device_catalog: the view re-joins five
        # identity tables per request and its ORDER BY spans three of them, so
        # the plan ended in USE TEMP B-TREE FOR ORDER BY over the whole
        # catalogue -- 404ms of the 404ms remaining at 53,436 devices. The flat
        # projection carries an index in exactly the grid's sort order.
        # Support is NOT joined here. It was a correlated scalar subquery in a
        # LEFT JOIN condition, which SQLite must evaluate for every candidate row
        # before ORDER BY can run -- 53,436 evaluations to render 100 rows, and
        # it also stopped the planner using device_catalog_flat_name_idx, so the
        # whole catalogue went through a temp B-tree twice. It is fetched for the
        # page's own ids below instead.
        #
        # Not folded into the projection like silicon, deliberately: an assertion
        # is valid over a time WINDOW, so a nightly-built copy would be wrong for
        # up to a day every time one opens or expires. Support status is the one
        # column here that has to be evaluated against now().
        base = f"""FROM device_catalog_flat dc
               -- Both of these were computed per request: a row_number() window
               -- over the whole projection to pick one row per device, and a
               -- GROUP BY over the whole projection for the totals -- each in
               -- full before LIMIT discarded almost all of it. Invisible at
               -- 1,271 rows, 1,549ms on a corpus scaled to the shape production
               -- grows into (53,436 devices). Both now come from the row the
               -- build marked primary, found through a partial index.
               LEFT JOIN device_current_firmware lf
                 ON lf.hardware_model_id=dc.hardware_model_id AND lf.is_device_primary=1
               WHERE {' AND '.join(clauses) if clauses else '1=1'}"""
        limit, offset = _pagination(query)
        # Default is alphabetical, deliberately.
        #
        # Sorting by "latest firmware" across the whole catalogue means ordering
        # one publisher's dates against another's, and here they do not measure
        # the same thing: a Samsung row's effective_at is when WE OBSERVED the
        # manifest declare a build latest (2026-09), while a Xiaomi row's is the
        # VENDOR'S OWN STATED RELEASE DATE (2025-11). Ranking those together put
        # all 83 Samsungs above all 89 Xiaomis and called it recency. It is the
        # same shape of error as the search that only read page one: a plausible
        # ordering that quietly answers a different question.
        #
        # So latest_desc now bands by what the date means before ordering within
        # the band, and the grid prints the basis on every row.
        sort = _first(query, "sort", "name_asc")
        banded = ("CASE lf.effective_at_basis WHEN 'source_observed' THEN 0 "
                  "WHEN 'vendor_stated_date' THEN 1 WHEN 'build_identifier_month' THEN 2 ELSE 3 END,"
                  "latest_firmware_at IS NULL,latest_firmware_at DESC,dc.brand,dc.variant,dc.model_code")
        order = {"latest_desc": banded,
                 "name_asc": "dc.brand,dc.variant,dc.model_code",
                 "android_desc": "lf.android_major IS NULL,lf.android_major DESC,dc.brand,dc.variant"}.get(
                     sort, "dc.brand,dc.variant,dc.model_code")
        rows = self.corpus.connection.execute(
            # No count(*) OVER() here. A window over the result set forces SQLite
            # to materialise EVERY matching row before LIMIT can discard any, so
            # the plan ended in USE TEMP B-TREE FOR ORDER BY even once both
            # projections were seekable. Measured at 53,436 devices: 89ms with
            # the window, 0.1ms without it plus 1.8ms for the count as its own
            # query. The separate count below was already there as a fallback.
            """SELECT dc.hardware_model_id id,dc.brand maker, dc.variant name, dc.model_code model,
                      dc.chip_marketing_name chip, dc.chip_part_number part,
                      dc.silicon_part_count part_count,
                      lf.android_version android, lf.latest_basis software_state_basis,
                      lf.security_patch_level patch, lf.security_patch_level_source_id patch_source,
                      lf.build_id build, lf.effective_at_basis date_basis, lf.fact_layer fact_layer,
                      lf.device_target_codes region, lf.device_target_total target_count,
                      lf.source_id build_source, lf.device_source_count source_count,
                      coalesce(lf.device_release_total,0) firmware_count,
                      lf.effective_at latest_firmware_at """
            # No GROUP BY. It was needed when this query joined hardware_silicon
            # and v_chip_devices, which fan out per device. Both projections are
            # now exactly one row per device -- device_catalog_flat by primary
            # key, device_current_firmware by the is_device_primary flag that
            # _validate() proves unique -- so grouping only forced SQLite to scan
            # by hardware_model_id and then sort the whole catalogue in a temp
            # B-tree. Without it the planner walks device_catalog_flat_name_idx
            # in order and stops at LIMIT.
            + base + f" ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
        total = self.corpus.connection.execute(
            "SELECT count(*) " + base, params).fetchone()[0]
        support_by_device = self._support_for(row["id"] for row in rows)
        # Firmware counts come from _firmware_holdings, NOT from the projection
        # column selected above, so this grid and the device detail view cannot
        # disagree about how many builds a device has. See _firmware_holdings.
        holdings = self._firmware_holdings(row["id"] for row in rows)
        result = [
            # The Android version and patch level stay suppressed when the only
            # basis is capture order. They describe the build we picked, and on
            # that basis we do not know the picked build is the one the device
            # is running -- so stating its version would be asserting a current
            # software state we have not established. The build itself is still
            # shown in the next column, under a label saying how it was chosen.
            # Costs 3 devices of 303; 86 keep a version via vendor_release_date.
            {**dict(row), "android": (row["android"] or "Unknown") if row["software_state_basis"] != "observation_order_only" else "Unknown",
             "patch": (row["patch"] or "Unknown") if row["software_state_basis"] != "observation_order_only" else "Unknown",
             # A device holding captured releases that none of the projection
             # would call current used to fall through to "firmware not
             # observed", which contradicted its own detail view. It now says
             # which of the two it is.
             "region": row["region"] or self.COVERAGE_NOTE.get(
                 holdings.get(row["id"], {}).get("firmwareCoverage"),
                 self.COVERAGE_NOTE[self.COVERAGE_NOT_OBSERVED]),
             **holdings.get(row["id"], {"firmware_count": 0,
                                        "firmwareCoverage": self.COVERAGE_NOT_OBSERVED}),
             **support_by_device.get(row["id"], {"support_status": None, "support_evidence_id": None,
                                                 "support_asserted_at": None}),
             "support": {v: k for k, v in support_codes.items()}.get(
                 (support_by_device.get(row["id"]) or {}).get("support_status"), "Unknown"),
             "confidence": "Demonstration" if self.demonstration else "Reviewed identity"}
            for row in rows
        ]
        for item in result:
            item.pop("_total",None)  # kept: other page builders still window
        return QueryPage(result, total, limit, offset)

    def chips(self, query: dict[str, list[str]]) -> list[dict]:
        return self.chips_page(query).items

    def chips_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses, params = _sql_filters(query, {"vendor": "vendor", "family": "family", "part": "part"},
                                      ("vendor", "family", "name", "part", "product_names"))
        index = """WITH
          hardware_counts AS (SELECT part_id,count(DISTINCT hardware_model_id) n
            FROM hardware_silicon GROUP BY part_id),
          product_counts AS (SELECT vendor,part_number,count(DISTINCT ops.product_id) n,
            group_concat(DISTINCT prod.canonical_name) names
            FROM observed_product_silicon ops JOIN source_products prod ON prod.id=ops.product_id
            WHERE vendor IS NOT NULL AND part_number IS NOT NULL AND prod.review_state!='rejected' GROUP BY vendor,part_number),
          security_counts AS (SELECT ac.subject_id part_id,count(DISTINCT ac.vulnerability_id) advisories,
            count(DISTINCT CASE WHEN fc.vulnerability_id IS NULL THEN ac.vulnerability_id END) open
            FROM applicability_claims ac LEFT JOIN (SELECT DISTINCT vulnerability_id FROM fix_claims) fc ON fc.vulnerability_id=ac.vulnerability_id
            WHERE ac.subject_type='silicon_part' AND ac.relationship='affected' GROUP BY ac.subject_id),
          chip_index AS (
          SELECT sp.id chip_key,sv.canonical_name vendor,sf.canonical_name family,
            sp.marketing_name name,sp.part_number part,
            coalesce(hc.n,0) canonical_devices,coalesce(pc.n,0) product_devices,
            pc.names product_names,coalesce(sc.advisories,0) advisories,coalesce(sc.open,0) open
          FROM silicon_parts sp JOIN silicon_families sf ON sf.id=sp.family_id
          JOIN silicon_vendors sv ON sv.id=sf.vendor_id
          LEFT JOIN hardware_counts hc ON hc.part_id=sp.id
          LEFT JOIN product_counts pc ON pc.vendor=sv.canonical_name COLLATE NOCASE
            AND pc.part_number=sp.part_number COLLATE NOCASE
          LEFT JOIN security_counts sc ON sc.part_id=sp.id
          UNION ALL
          SELECT 'product:'||lower(pc.vendor||':'||pc.part_number),pc.vendor,pc.vendor,
            max(ops.marketing_name),pc.part_number,0,pc.n,pc.names,0,0
          FROM product_counts pc JOIN observed_product_silicon ops
            ON ops.vendor=pc.vendor AND ops.part_number=pc.part_number
          WHERE NOT EXISTS (
            SELECT 1 FROM silicon_parts sp JOIN silicon_families sf ON sf.id=sp.family_id
            JOIN silicon_vendors sv ON sv.id=sf.vendor_id
            WHERE sv.canonical_name=pc.vendor COLLATE NOCASE AND sp.part_number=pc.part_number COLLATE NOCASE)
          GROUP BY pc.vendor,pc.part_number)"""
        where = " AND ".join(clauses) if clauses else "1=1"
        limit, offset = _pagination(query)
        sort = _first(query, "sort", "mobile_desc")
        order = {"mobile_desc": "(canonical_devices+product_devices)>0 DESC,(canonical_devices+product_devices) DESC,advisories DESC,vendor,name,part",
                 "devices_desc": "(canonical_devices+product_devices) DESC,vendor,name,part",
                 "advisories_desc": "advisories DESC,(canonical_devices+product_devices) DESC,vendor,name,part",
                 "name_asc": "vendor,name,part"}.get(sort, "(canonical_devices+product_devices)>0 DESC,(canonical_devices+product_devices) DESC,advisories DESC,vendor,name,part")
        rows = self.corpus.connection.execute(index + f""" SELECT *,canonical_devices+product_devices devices,
          count(*) OVER() _total
          FROM chip_index WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?""", [*params,limit,offset]).fetchall()
        total = rows[0]["_total"] if rows else self.corpus.connection.execute(
            index + f" SELECT count(*) FROM chip_index WHERE {where}", params).fetchone()[0]
        return QueryPage([{k: value for k, value in dict(row).items() if k != "_total"}
                          for row in rows], total, limit, offset)

    def chip_products_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses, params = [], []
        for key, column in (('vendor', 'ops.vendor'), ('part', 'ops.part_number')):
            value = _first(query, key).strip()
            if value:
                clauses.append(f'{column}=? COLLATE NOCASE')
                params.append(value)
        where = ' AND '.join(clauses) if clauses else '1=1'
        base = f'''FROM observed_product_silicon ops JOIN source_products sp ON sp.id=ops.product_id
                   WHERE sp.review_state!='rejected' AND {where}'''
        total = self.corpus.connection.execute(f'SELECT count(*) {base}', params).fetchone()[0]
        limit, offset = _pagination(query)
        rows = self.corpus.connection.execute(f'''SELECT sp.id,sp.canonical_name name,sp.manufacturer maker,
            ops.vendor,ops.part_number part,ops.confidence,ops.observed_at,ops.evidence_json
            {base} ORDER BY sp.canonical_name,sp.id LIMIT ? OFFSET ?''', [*params,limit,offset]).fetchall()
        return QueryPage([{**{k:v for k,v in dict(r).items() if k!='evidence_json'},
                           'evidence': json.loads(r['evidence_json'])} for r in rows], total, limit, offset)

    def product_detail(self, product_id: str) -> dict:
        db = self.corpus.connection
        row = db.execute('SELECT * FROM source_products WHERE id=?', (product_id,)).fetchone()
        if row is None:
            raise KeyError(product_id)
        product = dict(row)
        product['specification'] = json.loads(product.pop('specification_json') or 'null')
        identities = [dict(r) for r in db.execute('''SELECT sir.*,s.name source_name,s.base_url source_url
            FROM source_identity_registry sir JOIN sources s ON s.id=sir.source_id
            WHERE product_id=? ORDER BY namespace,source_value''', (product_id,))]
        conclusion = db.execute('SELECT * FROM identity_conclusions WHERE product_id=?', (product_id,)).fetchone()
        conclusion = dict(conclusion) if conclusion else None
        if conclusion:
            conclusion['evidence'] = json.loads(conclusion.pop('evidence_json'))
            conclusion['candidates'] = json.loads(conclusion.pop('candidates_json'))
        silicon = db.execute('SELECT * FROM observed_product_silicon WHERE product_id=?', (product_id,)).fetchone()
        silicon = dict(silicon) if silicon else None
        if silicon:
            silicon['evidence'] = json.loads(silicon.pop('evidence_json'))
        upgrades = []
        for event in db.execute(f'''SELECT * FROM {self._current_event_source()} WHERE subject_type='source_product'
            AND subject_id=? AND event_type='android_version_changed' ORDER BY occurred_at DESC,id''', (product_id,)):
            upgrades.append({'id': event['id'], 'effective_at': event['occurred_at'],
                'observed_at': event['recorded_at'], 'before': json.loads(event['before_json']),
                'after': json.loads(event['after_json'])})
        regions = [dict(r) for r in db.execute('''SELECT region_code region,channel,count(*) releases
            FROM product_firmware_releases WHERE product_id=? GROUP BY region_code,channel
            ORDER BY region_code,channel''', (product_id,))]
        hardware_link = db.execute('''SELECT phl.hardware_model_id,phl.model_code_source,dc.model_code,dc.brand,dc.variant
            FROM product_hardware_links phl JOIN v_device_catalog dc ON dc.hardware_model_id=phl.hardware_model_id
            WHERE phl.product_id=?''', (product_id,)).fetchone()
        return {'product': product, 'identities': identities, 'identityConclusion': conclusion,
            'silicon': silicon, 'androidUpgrades': upgrades, 'regions': regions,
            'lastObserved': max([i['last_seen_at'] for i in identities] +
                                ([silicon['observed_at']] if silicon else []) + [product['created_at']]),
            'firmware': _page_payload(self.product_releases_page({'product':[product_id], 'limit':['50']}), self.meta),
            'security': _page_payload(self.product_security_page({'product':[product_id], 'limit':['50']}), self.meta),
            'sourceBuilds': _page_payload(self.product_source_builds_page({'product':[product_id], 'limit':['50']}), self.meta),
            'hardware': dict(hardware_link) if hardware_link else None,
            'coverage': {'identity': 'product_only',
                         'hardware': 'established' if hardware_link else 'not_established',
                         'securityApplicability': 'not_established'}, 'meta': self.meta}

    def product_source_builds_page(self, query: dict[str,list[str]]) -> QueryPage:
        from .google_builds import product_source_builds
        limit,offset=_pagination(query)
        rows,total=product_source_builds(self.corpus.connection,_first(query,'product'),limit=limit,offset=offset)
        return QueryPage(rows,total,limit,offset)

    def releases(self, query: dict[str, list[str]]) -> list[dict]:
        return self.releases_page(query).items

    def source_records_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses = ["1=1"]
        params: list[object] = []
        q = _first(query, "q").strip()
        source = _first(query, "source").strip()
        kind = _first(query, "kind").strip()
        if q:
            clauses.append("(" + " OR ".join(like_clause(column) for column in (
                "o.source_key", "o.payload_json", "o.source_id")) + ")")
            params.extend([like_contains(q)] * 3)
        if source:
            clauses.append(like_clause("o.source_id"))
            params.append(like_contains(source))
        if kind:
            clauses.append("o.record_type=?")
            params.append(kind)
        where = " AND ".join(clauses)
        total = self.corpus.connection.execute(f"SELECT count(*) FROM observations o WHERE {where}", params).fetchone()[0]
        limit, offset = _pagination(query)
        sort = _first(query, "sort", "latest_desc")
        # Qualified with o. so the planner matches observations_effective_at_idx
        # rather than sorting the SELECT alias through a temp B-tree.
        order = {"latest_desc": "o.effective_at DESC,o.observed_at DESC",
                 "oldest_asc": "o.effective_at ASC,o.observed_at ASC",
                 "source_asc": "o.source_id,o.source_key,o.effective_at DESC",
                 "name_asc": "source_name,device,model_code,o.source_key"}.get(sort, "o.effective_at DESC,o.observed_at DESC")
        rows = self.corpus.connection.execute(f"""SELECT o.id, o.source_id source, o.record_type kind,
          o.source_key, o.observed_at, o.validation_state,
          json_extract(o.payload_json,'$.data.source_device_name') source_name,
          json_extract(o.payload_json,'$.data.device') device,
          json_extract(o.payload_json,'$.data.model_code') model_code,
          json_extract(o.payload_json,'$.data.region_code') region,
          json_extract(o.payload_json,'$.data.build') build,
          json_extract(o.payload_json,'$.data.android') android,
          json_extract(o.payload_json,'$.data.aspl_month') patch,
          o.effective_at,
          json_extract(o.payload_json,'$.data.identity_state') identity_state
          ,
          -- Whether this evidence row can be opened as a canonical device. The UI
          -- cannot work this out: device_detail() resolves a model_code against
          -- v_device_catalog, and the browser only holds the current page of
          -- devices. Answering here is what lets the table link the rows that CAN
          -- be opened while leaving the unresolved ones as plain text, instead of
          -- the current all-or-nothing where nothing is clickable.
          --
          -- Both of these used to be correlated subqueries, evaluated once per
          -- OUTPUT ROW: the first planned as a five-table `SCAN hm` through
          -- v_device_catalog, the second as `SCAN sp` over 2,347 products. At
          -- limit=100 that is 200 scans to render one page, and it grew with
          -- page size. As joins each lookup table is built once per query.
          hmx.model_code canonical_model,
          -- Fallback for rows that name a PRODUCT rather than a hardware model
          -- (the Transsion vendor feeds): link to the product record instead.
          -- `canonical_name <> manufacturer` drops the degenerate case where
          -- a row's device name is just the brand -- "TECNO" does match a product
          -- literally named TECNO, and linking every such row to it would be a
          -- confident link to the wrong thing.
          spx.id canonical_product,
          json_extract(o.payload_json,'$.data.download_url') download_url,
          coalesce(json_extract(o.payload_json,'$.data.source_url'),a.source_url,s.base_url) source_url
          FROM observations o JOIN artifacts a ON a.id=o.artifact_id JOIN sources s ON s.id=o.source_id
          -- GROUP BY, not a bare join: the subqueries carried LIMIT 1, and two
          -- source_products share a canonical_name case-insensitively. Without
          -- collapsing them a matching row would be DUPLICATED in the page and
          -- silently inflate the list against its own total.
          LEFT JOIN (SELECT model_code FROM hardware_models
                      GROUP BY model_code COLLATE NOCASE) hmx
            ON hmx.model_code = json_extract(o.payload_json,'$.data.model_code') COLLATE NOCASE
          LEFT JOIN (SELECT min(id) id, canonical_name FROM source_products
                      WHERE canonical_name <> manufacturer COLLATE NOCASE
                      GROUP BY canonical_name COLLATE NOCASE) spx
            ON spx.canonical_name = json_extract(o.payload_json,'$.data.device') COLLATE NOCASE
          WHERE {where}
          ORDER BY {order},o.source_id,o.source_key LIMIT ? OFFSET ?""",
          [*params, limit, offset]).fetchall()
        return QueryPage([dict(row) for row in rows], total, limit, offset)

    def source_products_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses = ["1=1"]
        params: list[object] = []
        for key, expression in (("maker", "sp.manufacturer"), ("state", "sp.review_state")):
            value = _first(query, key).strip()
            if value:
                if key == "region":
                    clauses.append(like_clause(expression)); params.append(like_contains(value))
                elif key == "state" and value == "not_approved":
                    # The review inbox's default. An exact-state filter could show
                    # 'proposed' OR 'unresolvable_on_captured_evidence' but never
                    # both, and the tab that exists to show what is not serving would
                    # then hide 626 of its 626 rows the moment they were adjudicated.
                    # Each row still carries and renders its OWN state, so the two
                    # populations are together without being merged.
                    clauses.append("sp.review_state <> 'approved'")
                else:
                    clauses.append(f"{expression}=? COLLATE NOCASE"); params.append(value)
        q = _first(query, "q").strip()
        if q:
            clauses.append("(" + " OR ".join(like_clause(column) for column in (
                "sp.canonical_name", "sir.source_value")) + ")")
            params.extend([like_contains(q)] * 2)
        where = " AND ".join(clauses)
        base = f"""FROM source_products sp LEFT JOIN source_identity_registry sir ON sir.product_id=sp.id
                   LEFT JOIN observation_product_links opl ON opl.product_id=sp.id
                   LEFT JOIN identity_conclusions ic ON ic.product_id=sp.id WHERE {where}"""
        total = self.corpus.connection.execute(f"SELECT count(DISTINCT sp.id) {base}", params).fetchone()[0]
        limit, offset = _pagination(query)
        # conclusion/method ride along because review_state alone cannot tell the two
        # unresolvable populations apart -- "no independent identifier exists" and
        # "several candidates exist and none discriminates" are different claims
        # about the world, and the row is where a reader meets them.
        rows = self.corpus.connection.execute(f"""SELECT sp.id,sp.manufacturer maker,sp.canonical_name name,
          sp.review_state, ic.conclusion, ic.method conclusion_method,
          json_extract(sp.specification_json,'$.chipset') chipset,
          json_extract(sp.specification_json,'$.os') launch_os,
          count(DISTINCT sir.id) identities,count(DISTINCT opl.observation_id) observations,
          min(sir.first_seen_at) first_seen,max(sir.last_seen_at) last_seen,
          group_concat(DISTINCT sir.source_value) source_values
          {base} GROUP BY sp.id ORDER BY last_seen DESC,sp.manufacturer,sp.canonical_name LIMIT ? OFFSET ?""",
          [*params, limit, offset]).fetchall()
        return QueryPage([dict(row) for row in rows], total, limit, offset)

    def _apply_product_review(self, product_id: str, decision: str) -> None:
        with self.corpus.transaction():
            self.corpus.connection.execute("UPDATE source_products SET review_state=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                           (decision, product_id))
            self.corpus.connection.execute("UPDATE source_identity_registry SET resolution_state=?,resolution_method='manual_product_review' WHERE product_id=?",
                                           (decision, product_id))
            # link_state is derived from the identity rows just written, not set
            # in parallel with them. The previous line approved EVERY link on the
            # product; a link whose identity belongs to another product would be
            # approved by a decision that never mentioned it. See
            # identity_bridge.LINK_LICENCE_CONDITION.
            refresh_observation_link_states(self.corpus.connection, product_id=product_id)
            if decision == "rejected":
                # A rejected identity must stop asserting a device relationship.
                # Without this the product kept its product_hardware_links row
                # and its firmware kept the hardware_model_id promotion had
                # backfilled, so a device went on serving builds attributed to
                # an identity a reviewer had just said was wrong.
                #
                # The DEVICE itself is left alone: several products may describe
                # one device, and deleting it because one of them was rejected
                # would take the others' firmware with it. What is withdrawn is
                # this product's claim.
                self.corpus.connection.execute(
                    """INSERT INTO source_data_corrections
                         (id, entity_type, entity_id, reason, before_json, after_json,
                          evidence_id, recorded_at)
                       SELECT ?, 'source_product', l.product_id, 'identity_rejected_link_withdrawn',
                              json_object('hardware_model_id', l.hardware_model_id,
                                          'model_code_source', l.model_code_source),
                              json_object('hardware_model_id', NULL), NULL,
                              strftime('%Y-%m-%dT%H:%M:%fZ','now')
                         FROM product_hardware_links l WHERE l.product_id=?""",
                    (str(__import__("uuid").uuid4()), product_id))
                self.corpus.connection.execute(
                    "UPDATE product_firmware_releases SET hardware_model_id=NULL WHERE product_id=?",
                    (product_id,))
                self.corpus.connection.execute(
                    "DELETE FROM product_hardware_links WHERE product_id=?", (product_id,))

    def _reapply_product_reviews(self) -> None:
        # Local human decisions survive replacement of the derived corpus.
        # Reapply only this exact source-product workflow; no hardware identities
        # or accepted agent proposals are promoted by startup.
        with self.local_lock:
            decisions = self.local.execute("SELECT canonical_id,decision FROM identity_decisions WHERE source_namespace='source_product' AND canonical_type='source_product'").fetchall()
        for row in decisions:
            product = self.corpus.connection.execute("SELECT review_state FROM source_products WHERE id=?", (row["canonical_id"],)).fetchone()
            if product:
                decision = {"same":"approved", "different":"rejected", "defer":"proposed"}[row["decision"]]
                self._apply_product_review(row["canonical_id"], decision)

    def review_source_product(self, product_id: str, decision: str) -> dict:
        if decision not in ("approved", "rejected", "proposed"):
            raise ValueError("invalid product decision")
        if self.corpus.connection.execute("SELECT 1 FROM source_products WHERE id=?", (product_id,)).fetchone() is None:
            raise KeyError(product_id)
        with self.local_lock:
            # Record intent first so a power loss can replay the review projection.
            save_decision(self.local, {"sourceNamespace":"source_product", "sourceValue":product_id,
                "canonicalType":"source_product", "canonicalId":product_id,
                "decision":{"approved":"same", "rejected":"different", "proposed":"defer"}[decision],
                "rationale":"Explicit manual source-product review; no hardware identity promotion"})
            self._apply_product_review(product_id, decision)
        # Republish: a review changes which devices serve which firmware, and
        # the read path serves the projection. Without this a rejected identity
        # kept its builds on the grid until the next nightly batch -- the
        # reviewer's decision was recorded and not acted on.
        try:
            build_current_firmware(self.corpus)
        except ProjectionError:
            # The decision is committed and correct; the projection is stale and
            # check_corpus will say so rather than this failing the review.
            pass
        return {"ok": True, "productId": product_id, "decision": decision}

    def device_detail(self, model: str) -> dict:
        c = self.corpus.connection
        device = c.execute("SELECT * FROM v_device_catalog WHERE model_code=? COLLATE NOCASE", (model,)).fetchone()
        if device is None:
            raise KeyError(model)
        device = dict(device)
        model = device['model_code']
        identifier = device['hardware_model_id']
        silicon = [dict(row) for row in c.execute("SELECT * FROM v_chip_devices WHERE hardware_model_id=? ORDER BY role,part_number", (identifier,))]
        aliases = [dict(row) for row in c.execute("SELECT namespace,alias,review_state FROM aliases WHERE entity_type='hardware_model' AND entity_id=? ORDER BY namespace,alias", (identifier,))]
        regions = [dict(row) for row in c.execute("SELECT target_code region,channel,count(*) count FROM v_device_region_history WHERE hardware_model_id=? GROUP BY target_code,channel ORDER BY target_code,channel", (identifier,))]
        # Reads the projection, so this panel now answers for every brand rather
        # than only the one whose firmware lives in firmware_releases. The
        # observation_order_only gate is kept: capture order alone is not a
        # claim that a build is current, and saying nothing is the honest
        # outcome there.
        latest = [dict(row) for row in c.execute('''SELECT build_id build,target_key region,channel,
            android_version android,security_patch_level patch,security_patch_level_source_id patch_source,
            latest_basis,effective_at,effective_at_basis,release_count,source_id
            FROM device_current_firmware
            WHERE hardware_model_id=? AND latest_basis!='observation_order_only'
            ORDER BY target_key,channel''', (identifier,))]
        from .lineage_specs import hardware_specification_evidence
        holdings = self._firmware_holdings([identifier]).get(
            identifier, {'firmware_count': 0, 'firmwareCoverage': self.COVERAGE_NOT_OBSERVED})
        return {'latestFirmware': latest, 'device': device, 'silicon': silicon, 'aliases': aliases, 'regions': regions,
                'specifications': hardware_specification_evidence(c, model),
                # ROM history. The canonical view behind releases_page is fed by
                # one source and covers Samsung only, so an evidence-layer device
                # -- every TECNO, itel, Infinix and Xiaomi -- got an empty
                # history panel while the grid beside it said "6 builds · 4
                # regions". Two views of one device disagreeing is the same
                # falsehood as "firmware not observed", one screen along.
                'firmware': _page_payload(self._device_history(model, identifier), self.meta),
                # The SAME answer the grid renders, from the same function -- so
                # a reader who sees "firmware not observed" on the grid cannot
                # then be shown a list of builds here, and vice versa. It is
                # published rather than merely used, because the two panels above
                # ('Latest known regional builds' and 'Captured ROM history')
                # legitimately hold different numbers of rows and this is what
                # says why.
                **holdings,
                'firmwareCoverageNote': self.COVERAGE_NOTE.get(holdings['firmwareCoverage']),
                'security': _page_payload(self.security_page({'model':[model],'limit':['50']}), self.meta),
                'boundaries': {'identity': 'Reviewed hardware identity; incomplete specifications remain unknown.',
                               'history': 'All captured releases are accessible through pagination; this does not imply complete vendor coverage.',
                               'security': 'Containing a claimed affected part does not establish a firmware verdict. No linked findings is not proof of safety.'},
                'meta': self.meta}

    def _device_history(self, model: str, hardware_model_id: str) -> QueryPage:
        """Captured releases for one device, from whichever layer holds them.

        A device lives in exactly one layer -- the projection refuses to publish
        otherwise -- so this prefers the canonical history and falls back to the
        evidence layer rather than merging two grains. The evidence rows are
        shaped to the canonical payload so the existing renderer needs no
        special case: a missing value stays missing rather than being filled.
        """
        canonical = self.releases_page({'model_exact': [model], 'limit': ['50']})
        if canonical.items:
            return canonical
        rows = self.corpus.connection.execute(
            """SELECT pfr.id, sp.manufacturer maker, sp.canonical_name device,
                      ? model, pfr.region_code region, pfr.build_id build,
                      pfr.android_version android, NULL patch, NULL baseband,
                      pfr.vendor_released_at released,
                      pfr.created_at observed, pfr.channel,
                      s.base_url source_url, NULL build_derived_month,
                      CASE WHEN pfr.vendor_released_at IS NULL
                           THEN 'not_captured' ELSE 'vendor_stated_date' END date_basis,
                      NULL evidence_id, pfr.source_id source
                 FROM product_firmware_releases pfr
                 JOIN source_products sp ON sp.id = pfr.product_id
                 LEFT JOIN sources s ON s.id = pfr.source_id
                WHERE pfr.hardware_model_id = ?
                ORDER BY pfr.vendor_released_at IS NULL,
                         pfr.vendor_released_at DESC,
                         pfr.created_at DESC, pfr.id DESC
                LIMIT 50""", (model, hardware_model_id)).fetchall()
        total = self.corpus.connection.execute(
            "SELECT count(*) FROM product_firmware_releases WHERE hardware_model_id=?",
            (hardware_model_id,)).fetchone()[0]
        return QueryPage([dict(row) for row in rows], total, 50, 0)

    def releases_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses, params = _sql_filters(query, {"maker": "brand", "region": "target_code",
                                               "model": "model_code", "channel": "channel"},
                                      ("brand", "variant", "model_code", "codename", "target_code", "build_id", "baseband_version"))
        for key, column in (('model_exact','model_code'),('region_exact','target_code'),('channel_exact','channel')):
            value = _first(query,key).strip()
            if value:
                clauses.append(f'{column}=? COLLATE NOCASE');params.append(value)
        where = " AND ".join(clauses) if clauses else "1=1"
        total = self.corpus.connection.execute(
            f"SELECT count(*) FROM v_device_region_history WHERE {where}", params).fetchone()[0]
        limit, offset = _pagination(query)
        sort = _first(query, "sort", "latest_desc")
        order = {"latest_desc": "coalesce(vendor_released_at,first_observed_at) DESC,variant,target_code,firmware_release_id DESC",
                 "oldest_asc": "coalesce(vendor_released_at,first_observed_at) ASC,variant,target_code,firmware_release_id",
                 "name_asc": "brand,variant,target_code,coalesce(vendor_released_at,first_observed_at) DESC",
                 "android_desc": "os_major IS NULL,os_major DESC,coalesce(vendor_released_at,first_observed_at) DESC"}.get(
                     sort, "coalesce(vendor_released_at,first_observed_at) DESC,variant,target_code,firmware_release_id DESC")
        rows = self.corpus.connection.execute(
            f"""SELECT firmware_release_id id, brand maker, variant device, model_code model,
                       target_code region, build_id build, os_major android,
                       security_patch_level patch, baseband_version baseband,
                       vendor_released_at released, first_observed_at observed, channel,
                       (SELECT max(a.source_url) FROM firmware_release_evidence fre
                        JOIN evidence e ON e.id=fre.evidence_id JOIN artifacts a ON a.id=e.artifact_id
                        WHERE fre.firmware_release_id=v_device_region_history.firmware_release_id) source_url
                FROM v_device_region_history WHERE {where}
                ORDER BY {order} LIMIT ? OFFSET ?""", [*params, limit, offset]).fetchall()
        result = [{**dict(row), "android": row["android"] or "Unknown",
                         "patch": row["patch"] or "Unknown", "baseband": row["baseband"] or "Unknown"}
                        for row in rows]
        # One batched lookup for the whole page. This used to call the single-row
        # firmware_date_evidence() per item, so a 100-row page cost ~200 extra
        # queries before it could render.
        from .source_corrections import firmware_date_evidence_many
        evidence = firmware_date_evidence_many(self.corpus.connection, [i["id"] for i in result])
        for item in result:
            item.update(evidence.get(item["id"], {}))
        return QueryPage(result, total, limit, offset)

    def product_releases_page(self, query: dict[str, list[str]]) -> QueryPage:
        """A page of product firmware evidence.

        Two steps, because the ORDER BY was the entire cost of this route. A
        temp b-tree sorted all 27,841 JOINED rows to return 100: dropping the
        ORDER BY took the row query from 86.5ms to 0.9ms, while dropping all four
        json_extract calls took it from 86.5ms to 77.8ms -- so payload parsing was
        never the cost, the sort was. The count over the same join set was another
        76.0ms. Measured on the live corpus, warm disk.

        So: decide WHICH releases with the narrowest join set the filters and the
        sort actually name, then join the payload tables to those. The big sort
        and the count now run over product_firmware_releases plus at most
        source_products, instead of over the full six-table join.

        This is row-equivalent ONLY because the payload joins are lossless -- one
        row in, one row out, never zero and never two. That is asserted, against
        real rows and against the live corpus, by
        tests/test_product_release_joins_are_lossless.py, which fails if a join is
        added that can duplicate or an orphan makes one drop. If it ever stops
        holding, this endpoint starts reporting a total that is not a count of
        releases and pages that render short -- a different answer, not a faster
        one.
        """
        clauses = ["1=1"]
        params: list[object] = []
        # Which aliases the SELECTION step needs: an alias is only joined for the
        # count and the sort if a filter or the ORDER BY actually mentions it.
        needed: set[str] = set()
        for key, expression in (("maker", "sp.manufacturer"), ("region", "pfr.region_code"),
                                ("channel", "pfr.channel"), ("product", "sp.id")):
            value = _first(query, key).strip()
            if value:
                clauses.append(f"{expression}=? COLLATE NOCASE"); params.append(value)
                if expression.startswith("sp."):
                    needed.add("sp")
        q = _first(query, "q").strip()
        if q:
            clauses.append("(" + " OR ".join(like_clause(column) for column in (
                "sp.canonical_name", "pfr.build_id", "sir.source_value")) + ")")
            params.extend([like_contains(q)] * 3)
            needed.update(("sp", "sir"))
        where = " AND ".join(clauses)
        limit, offset = _pagination(query)
        sort = _first(query, "sort", "released_desc")
        order = {"released_desc": "pfr.vendor_released_at IS NULL,pfr.vendor_released_at DESC",
                 "released_asc": "pfr.vendor_released_at IS NULL,pfr.vendor_released_at ASC",
                 "product_asc": "sp.canonical_name COLLATE NOCASE ASC",
                 "android_desc": "CAST(pfr.android_version AS INTEGER) DESC"}.get(
                     sort, "pfr.vendor_released_at IS NULL,pfr.vendor_released_at DESC")
        if order.startswith("sp."):
            needed.add("sp")
        selection_joins = product_release_joins(needed)
        total = self.corpus.connection.execute(
            f"SELECT count(*) {selection_joins} WHERE {where}", params).fetchone()[0]
        # The page's ids, ordered. `pfr.id DESC` is the tiebreaker the payload
        # query repeats, so the two agree on the order within an equal sort key.
        page_ids = [row[0] for row in self.corpus.connection.execute(
            f"SELECT pfr.id {selection_joins} WHERE {where} "
            f"ORDER BY {order},pfr.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()]
        if not page_ids:
            return QueryPage([], total, limit, offset)
        joins = product_release_joins(PRODUCT_RELEASE_JOIN_ORDER)
        placeholders = ",".join("?" * len(page_ids))
        rows = self.corpus.connection.execute(f"""SELECT pfr.id,sp.id product_id,
          sp.manufacturer maker,sp.canonical_name device,sir.source_value source_identity,
          pfr.region_code region,pfr.build_id build,pfr.channel,pfr.android_version android,
          pfr.vendor_released_at released,pfr.delivery_method,pfr.source_id source,
          json_extract(o.payload_json,'$.data.security_patch_level') security_patch_level,
          json_extract(o.payload_json,'$.data.release_scope') release_scope,
          json_extract(o.payload_json,'$.data.comments') vendor_comments,
          json_extract(o.payload_json,'$.data.download_url') download_url,
          ar.source_url source_url,o.observed_at observed,
          coalesce(json_extract(ops.evidence_json,'$[0].slug'),json_extract(sp.specification_json,'$.slug')) spec_slug
          {joins} WHERE pfr.id IN ({placeholders})""", page_ids).fetchall()
        # Ordered by the selection step, not by a second ORDER BY over the joined
        # set -- which is the sort this rewrite exists to avoid paying twice.
        by_id = {row["id"]: dict(row) for row in rows}
        return QueryPage([by_id[i] for i in page_ids if i in by_id], total, limit, offset)

    def product_security_page(self, query: dict[str, list[str]]) -> QueryPage:
        clauses = ["1=1"]
        params: list[object] = []
        product_id = _first(query, "product").strip()
        if product_id:
            clauses.append("sp.id=?"); params.append(product_id)
        maker = _first(query, "maker").strip()
        if maker:
            clauses.append("sp.manufacturer=? COLLATE NOCASE"); params.append(maker)
        q = _first(query, "q").strip()
        if q:
            clauses.append("(" + " OR ".join(like_clause(column) for column in (
                "sp.canonical_name", "psp.security_patch_month")) + ")")
            params.extend([like_contains(q)] * 2)
        where = " AND ".join(clauses)
        joins = """FROM product_security_publications psp
          JOIN source_products sp ON sp.id=psp.product_id
          JOIN source_identity_registry sir ON sir.id=psp.identity_id
          JOIN observations o ON o.id=psp.observation_id
          JOIN artifacts ar ON ar.id=o.artifact_id
          LEFT JOIN observed_product_silicon ops ON ops.product_id=sp.id"""
        total = self.corpus.connection.execute(f"SELECT count(*) {joins} WHERE {where}",params).fetchone()[0]
        limit,offset = _pagination(query)
        sort = _first(query, "sort", "released_desc")
        order = {"released_desc": "psp.published_at IS NULL,psp.published_at DESC,psp.security_patch_month DESC",
                 "released_asc": "psp.published_at IS NULL,psp.published_at ASC,psp.security_patch_month ASC",
                 "product_asc": "sp.canonical_name COLLATE NOCASE ASC"}.get(
                     sort, "psp.published_at IS NULL,psp.published_at DESC,psp.security_patch_month DESC")
        rows = self.corpus.connection.execute(f"""SELECT psp.id,sp.id product_id,
          sp.manufacturer maker,sp.canonical_name device,sir.source_value source_identity,
          psp.security_patch_month patch,psp.published_at,psp.title,psp.source_id source,
          json_extract(o.payload_json,'$.data.security_patch_level') security_patch_level,
          json_extract(o.payload_json,'$.data.release_date') first_live_release,
          json_extract(o.payload_json,'$.data.release_scope') release_scope,
          coalesce(json_extract(o.payload_json,'$.data.source_url'),ar.source_url) source_url,
          coalesce(json_extract(ops.evidence_json,'$[0].slug'),json_extract(sp.specification_json,'$.slug')) spec_slug
          {joins} WHERE {where} ORDER BY {order},psp.id DESC LIMIT ? OFFSET ?""",
          [*params,limit,offset]).fetchall()
        return QueryPage([dict(row) for row in rows],total,limit,offset)

    def security_page(self, query: dict[str, list[str]]) -> QueryPage:
        from .security_read import query_catalog
        limit, offset = _pagination(query)
        rows, total = query_catalog(self.corpus.connection, query, limit, offset)
        return QueryPage(rows, total, limit, offset)

    def security_detail(self, cve: str) -> dict:
        from .security_read import detail
        return {**detail(self.corpus.connection, cve), 'meta': self.meta}

    def security_coverage(self) -> dict:
        rows = self.corpus.connection.execute(
            "SELECT vendor,capability,status,reason,evidence_uri,checked_at FROM security_coverage_gaps ORDER BY vendor"
        ).fetchall()
        return {"items": [dict(row) for row in rows], "meta": self.meta}

    def security(self, query: dict[str, list[str]]) -> list[dict]:
        return self.security_page(query).items

    IMPORT_CONTRACT = "\n\nImport contract: return a JSON array. confidence must be low, medium, or high. Every evidence entry must include note and at least one of url (HTTP/HTTPS), artifact_id, observation_id (existing captured IDs). No additional fields. Submit to POST /api/v1/identity/agent-proposals; every output is a proposal only. Approval remembers a review; it never promotes model codes, aliases, silicon, security, or firmware facts. Previously reviewed targets must not be proposed again.\n"

    def _agent_review_candidates(self) -> tuple[list, list, str]:
        """(candidates, remembered reviews, prompt template) or ([], [], "").

        The one place that reads the review bundle off disk, so the panel payload
        and the paste text cannot disagree about which candidates are outstanding.
        """
        root = self.data_dir / "agent-review"
        prompt = root / "agent-review-prompt.md"
        candidates = root / "identity-candidates.json"
        if not prompt.is_file() or not candidates.is_file():
            return [], [], ""
        candidate_data = json.loads(candidates.read_text(encoding="utf-8"))
        with self.local_lock:
            memory = [dict(r) for r in self.local.execute("SELECT product_id,status,reviewer,rationale,json_extract(payload_json,'$.canonical_name') canonical_name,json_extract(payload_json,'$.model_codes') model_codes FROM agent_proposals WHERE status!='pending' GROUP BY target_key")]
        remembered_products = {r["product_id"] for r in memory if r["status"] in ("approved","deferred")}
        candidate_data = [c for c in candidate_data if c.get("id",c.get("product_id")) not in remembered_products]
        for candidate in candidate_data:
            candidate["remembered_reviews"] = [r for r in memory if r["product_id"] == candidate.get("id",candidate.get("product_id"))]
        return candidate_data, memory, prompt.read_text(encoding="utf-8") + self.IMPORT_CONTRACT

    def agent_review_bundle(self) -> dict:
        """What the Admin handoff panel renders.

        `pastePrompt` is deliberately NOT here any more. It was 314,700 characters
        of a 615,142-byte response, and 313,159 of those characters were the same
        628 candidates this response already returns under `candidates`, re-encoded
        as indented JSON inside the prompt text. The actual prompt is 1,541
        characters. The panel renders a count and a candidate list and never
        displayed the paste text at all, so the response carried its largest field
        for a button that had not been pressed: 615,142 -> 265,518 bytes, -57%.

        The paste text still exists, byte for byte, at
        GET /api/v1/identity/agent-bundle/prompt, which the Copy button fetches
        when it is clicked. Assembled on the SERVER rather than concatenated in the
        browser on purpose: Python's json.dumps escapes non-ASCII and JavaScript's
        JSON.stringify does not, so a client-side join of the same two pieces would
        produce a DIFFERENT prompt for any candidate carrying a non-ASCII character
        -- a quietly corrupted agent assignment, which is worse than a large
        response.
        """
        candidate_data, memory, _template = self._agent_review_candidates()
        return {"candidateCount": len(candidate_data), "candidates": candidate_data,
                "rememberedReviews": memory}

    def agent_review_prompt(self) -> dict:
        """The complete paste text, fetched when the operator asks to copy it."""
        candidate_data, _memory, template = self._agent_review_candidates()
        if not template:
            return {"pastePrompt": ""}
        return {"pastePrompt": template + "\n\n# Candidate data\n```json\n"
                + json.dumps(candidate_data, indent=2) + "\n```\n"}

    def integrity(self) -> dict:
        """Corpus invariant findings, and what the projection is serving.

        Surfaced on an endpoint rather than only written to the batch log,
        because a finding nobody looks at is not a finding. The Admin page is
        where an operator already goes to ask whether a run worked.
        """
        # deep=False: the two whole-database page scans belong to the batch, not
        # to an endpoint the UI polls on every load. See check_corpus.
        #
        # Cached for a few seconds on top of that. Even the cheap set aggregates
        # the whole projection -- 855ms at 153,896 devices -- and the corpus it
        # examines changes when a batch runs, not between two page loads. A
        # short TTL rather than caching on the projection generation, because
        # one of these checks exists precisely to notice a change that did NOT
        # republish, and keying the cache on the generation would blind it.
        now = time.monotonic()
        with self.local_lock:
            cached = self._integrity_cache
        if cached is None or now - cached[0] > self.INTEGRITY_CACHE_SECONDS:
            findings = check_corpus(self.corpus.connection, deep=False)
            payload = {"summary": summarise(findings),
                       "findings": [f.as_dict() for f in findings],
                       "checkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                       "scope": "fast checks only; the whole-database scans run in the batch"}
            with self.local_lock:
                self._integrity_cache = (now, payload)
        else:
            payload = cached[1]
        return {"integrity": payload,
                "projection": current_firmware_state(self.corpus.connection),
                # Pending work, reported separately from faults. Without this a
                # vendor whose evidence is all awaiting review shows as a silent
                # zero and reads as "never collected".
                "reviewQueue": review_queue(self.corpus.connection)}

    def health(self) -> list[dict]:
        # Silence is advisory and computed independently of the last run's own
        # outcome: a source can end its last run "succeeded" and still be
        # overdue for its NEXT one. See silence.py and docs/SOURCE_SILENCE_DETECTION.md.
        silence_by_source = {f["source_id"]: f for f in detect_silence(self.corpus.connection)}
        rows = self.corpus.connection.execute("""SELECT s.id source_id,s.name source,s.authority_scope scope,
          ir.id run_id,ir.outcome status,ir.finished_at last,ir.accepted_count records,
          (SELECT max(a.retrieved_at) FROM artifacts a WHERE a.source_id=s.id) captured_at,
          (SELECT max(o.observed_at) FROM observations o WHERE o.source_id=s.id) observed_at
          FROM ingestion_runs ir JOIN sources s ON s.id=ir.source_id
          WHERE ir.id=(SELECT ir2.id FROM ingestion_runs ir2 WHERE ir2.source_id=ir.source_id
                       ORDER BY ir2.started_at DESC,ir2.id DESC LIMIT 1)
          ORDER BY s.name""").fetchall()
        if rows:
            return [{**dict(row), "next": "Manual only · no scheduler installed",
                     "execution_mode": "captured_replay" if row["run_id"].startswith("manual-") else "snapshot_import",
                     "live_network": False, "freshness": "Captured evidence; import success is not a vendor refresh",
                     **_silence_fields(silence_by_source.get(row["source_id"]))}
                    for row in rows]
        if not self.demonstration:
            return [{"source": r["name"], "scope": r["authority_scope"], "status": "No imported run",
                     "last": None, "next": "Manual only · no scheduler installed", "records": 0,
                     "captured_at": None, "observed_at": None, "execution_mode": "unknown", "live_network": False,
                     **_silence_fields(silence_by_source.get(r["id"]))}
                    for r in self.corpus.connection.execute("SELECT id,name,authority_scope FROM sources ORDER BY name")]
        return [{"source": "Synthetic demonstration fixture", "scope": "Sample only", "status": "Demo",
                 "last": DEMO_TIME, "next": "No collection scheduled", "records": "0",
                 "captured_at": None, "observed_at": None, "execution_mode": "demonstration", "live_network": False,
                 **_silence_fields(None)}]

    def overview(self) -> dict:
        with self.local_lock:
            acknowledged = {row[0] for row in self.local.execute("SELECT event_id FROM acknowledgements").fetchall()}
        event_ids = {row[0] for row in self.corpus.connection.execute(f"SELECT id FROM {self._radar_event_source()}")}
        if not event_ids and self.corpus.connection.execute("SELECT count(*) FROM domain_events").fetchone()[0] == 0:
            event_ids = {row[0] for row in self.corpus.connection.execute("SELECT id FROM firmware_releases")}
        health_rows = self.health()
        # A source can be "silent" (overdue for its next run) even while its
        # last recorded outcome reads "succeeded" -- OR, not add, so a source
        # that is both failed and overdue is not double-counted.
        failures = sum((r["status"] not in ("healthy", "succeeded", "Demo")) or r.get("silent", False)
                       for r in health_rows)
        android_upgrades = self.corpus.connection.execute(
            f"""SELECT count(*) FROM {self._radar_event_source()}
               WHERE event_type='android_version_changed'
                  OR (json_extract(before_json,'$.android') IS NOT NULL
                      AND json_extract(after_json,'$.android') IS NOT NULL
                      AND CAST(json_extract(before_json,'$.android') AS TEXT)
                          <> CAST(json_extract(after_json,'$.android') AS TEXT))"""
        ).fetchone()[0]
        return {**self.meta, "meta": self.meta, "unseen": len(event_ids - acknowledged), "androidUpgrades": android_upgrades,
                "securityPatches": self.corpus.connection.execute(f"SELECT count(*) FROM {self._radar_event_source()} WHERE event_type='security_patch_changed'").fetchone()[0],
                "securityPublications": self.corpus.connection.execute("SELECT count(*) FROM observations WHERE record_type='security_patch_publication'").fetchone()[0],
                # "security_patch_changed events" is honestly 0 here -- no patch
                # level was ever ingested, so none could be observed changing.
                # But a bare 0 on a security tool's front page reads as "this
                # fleet has no patches", which is the opposite of what it means.
                # These two say how much patch data is actually known, so the
                # zero can be read as coverage rather than as a finding.
                **self._patch_level_coverage(),
                "sourceWarnings": failures, "lastRun": self.corpus.connection.execute("SELECT max(finished_at) FROM ingestion_runs").fetchone()[0]}

    def _patch_level_coverage(self) -> dict:
        """How many devices have a known current security patch level, and the
        newest one seen. Absent projection is reported as absent, not as zero."""
        row = self.corpus.connection.execute(
            """SELECT count(DISTINCT hardware_model_id), max(security_patch_level)
                 FROM device_current_firmware WHERE security_patch_level IS NOT NULL"""
        ).fetchone()
        total = self.corpus.connection.execute(
            "SELECT count(*) FROM hardware_models").fetchone()[0]
        return {"patchLevelDevices": row[0], "patchLevelDeviceTotal": total,
                "newestPatchLevel": row[1]}

    def acknowledge(self, event_id: str) -> None:
        found = self.corpus.connection.execute(f"SELECT 1 FROM {self._radar_event_source()} WHERE id = ?", (event_id,)).fetchone()
        if found is None and self.corpus.connection.execute("SELECT count(*) FROM domain_events").fetchone()[0] == 0:
            found = self.corpus.connection.execute("SELECT 1 FROM firmware_releases WHERE id = ?", (event_id,)).fetchone()
        if found is None:
            raise KeyError(event_id)
        with self.local_lock:
            self.local.execute(
                "INSERT OR REPLACE INTO acknowledgements(event_id) VALUES (?)", (event_id,)
            )
            self.local.commit()

    # The cap on a bulk acknowledgement, in ids. It guards a REQUEST BODY, which
    # is why `capped` exists below: dismiss_unwatched() builds its list from this
    # server's own query, and on a freshly ingested corpus that is legitimately
    # every one of its 5,869 radar events. A class attribute rather than a
    # literal so a test can lower it and exercise both sides cheaply.
    BULK_ACKNOWLEDGE_LIMIT = 5000

    def acknowledge_many(self, event_ids: list[str], *, capped: bool = True) -> dict:
        # A str is iterable, so `for item in event_ids` over "abcdef" iterated
        # its SIX CHARACTERS: {"ids": "abcdef"} acknowledged nothing, hit "a" as
        # the first unknown id, and answered 404 update_not_found -- a type error
        # reported as a fact about the corpus. `{"ids": 7}` reached 400 only
        # because an int happens not to be iterable, so the correct answer was
        # an accident of which wrong type you sent.
        #
        # _require_id_list is shared with every other list-taking entry point for
        # the same reason _pagination is shared: this assumption is not specific
        # to acknowledgements.
        event_ids = _require_id_list(event_ids, "update ids")
        ids = list(dict.fromkeys(str(item).strip() for item in event_ids if str(item).strip()))
        if capped and len(ids) > self.BULK_ACKNOWLEDGE_LIMIT:
            raise ValueError("too many update ids")
        valid = {row[0] for row in self.corpus.connection.execute(f"SELECT id FROM {self._radar_event_source()}")}
        if not valid and self.corpus.connection.execute("SELECT count(*) FROM domain_events").fetchone()[0] == 0:
            valid = {row[0] for row in self.corpus.connection.execute("SELECT id FROM firmware_releases")}
        missing = [item for item in ids if item not in valid]
        if missing:
            raise KeyError(missing[0])
        with self.local_lock:
            self.local.executemany(
                "INSERT OR REPLACE INTO acknowledgements(event_id) VALUES (?)",
                [(item,) for item in ids],
            )
            self.local.commit()
        return {"acknowledged": len(ids)}

    def acknowledgements(self) -> list[str]:
        """Every acknowledged event id. Complete, and used as such internally."""
        with self.local_lock:
            return [row[0] for row in self.local.execute(
                "SELECT event_id FROM acknowledgements ORDER BY acknowledged_at DESC").fetchall()]

    def acknowledgements_page(self, query: dict[str, list[str]]) -> QueryPage:
        """A bounded page of acknowledged event ids, with the total beside it.

        The route used to return the whole table -- 5,785 bare UUIDs, 231,411
        bytes, and it compresses only 1.8x because a UUID is already dense, so
        this is a payload that has to get SMALLER rather than squeezed. It is the
        last list endpoint that was unbounded; the other twelve all go through
        _pagination, which exists because an uncapped limit turns one mistyped
        query string into a full-table render.

        Worth being exact about what this wins, because it is not what it looks
        like: nothing fetches this. api.acknowledgements() is defined in api.js and
        called from nowhere in app.js, and the boot waterfall is 22 calls of which
        this is not one -- the Radar feed seeds the acknowledged set from the rows
        actually on screen instead. So 231KB was never on the page-load path and
        capping it removes a LATENT cost, not a measured one.

        The page carries meta.page with the true total, so a truncated answer is
        never presented as the complete list. acknowledgements() above still
        returns everything and is what internal callers use.
        """
        limit, offset = _pagination(query)
        with self.local_lock:
            total = self.local.execute("SELECT count(*) FROM acknowledgements").fetchone()[0]
            rows = self.local.execute(
                "SELECT event_id FROM acknowledgements ORDER BY acknowledged_at DESC "
                "LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return QueryPage([row[0] for row in rows], total, limit, offset)

    # --- dismiss every unwatched item ----------------------------------------
    # Scoped to the tab and filters the operator currently has applied, NOT to
    # the whole corpus. Someone who has typed a maker into the search box is
    # looking at a subset and means that subset; "everything, including the
    # 5,700 rows you filtered away" is the answer nobody asks for and cannot
    # take back by re-typing the filter. It is the same predicate updates_page()
    # renders, so what disappears is exactly what was on screen.
    RADAR_FILTER_KEYS = ("q", "maker", "model", "region")

    def _unwatched_pending_ids(self, query: dict[str, list[str]]) -> list[str]:
        """Event ids matching the current view that are neither watched nor
        already dismissed. One query; the caller never enumerates ids."""
        watched, seen = self._radar_state()
        if self.corpus.connection.execute("SELECT count(*) FROM domain_events").fetchone()[0]:
            joins, where, params = self._event_feed_query(query, watched, seen)
            clause, watch_params = self._event_watch_clause(watched)
            rows = self.corpus.connection.execute(
                f"""SELECT DISTINCT de.id {joins} WHERE {where} AND NOT {clause}
                      AND de.id NOT IN (SELECT value FROM json_each(?))""",
                [*params, *watch_params, json.dumps(seen)]).fetchall()
        else:
            where, params = self._release_feed_query(query, watched, seen)
            clause, watch_params = self._release_watch_clause(watched)
            rows = self.corpus.connection.execute(
                f"""SELECT firmware_release_id FROM v_device_region_history
                     WHERE {where} AND NOT {clause}
                       AND firmware_release_id NOT IN (SELECT value FROM json_each(?))""",
                [*params, *watch_params, json.dumps(seen)]).fetchall()
        return [row[0] for row in rows]

    def unwatched_pending(self, query: dict[str, list[str]]) -> dict:
        """What a dismissal would do, for the confirm step to state before the
        click. Runs the SAME query the POST runs rather than a second estimate,
        so the number shown is the number acted on."""
        applied = sorted(key for key in self.RADAR_FILTER_KEYS if _first(query, key).strip())
        if _first(query, "change").strip():
            applied.append("change")
        return {"count": len(self._unwatched_pending_ids(query)),
                "tab": _first(query, "tab", "history"), "filters": applied,
                "scope": "this tab and these filters, excluding watched subjects"}

    def dismiss_unwatched(self, query: dict[str, list[str]]) -> dict:
        """Dismiss them, reusing acknowledge_many: one validity check, one
        executemany, one commit. `capped=False` because the cap guards an
        untrusted request body and this list came from the query above."""
        ids = self._unwatched_pending_ids(query)
        dismissed = self.acknowledge_many(ids, capped=False)["acknowledged"]
        with self.local_lock:
            self._undo_batch = list(ids)
        return {"dismissed": dismissed, "undoable": len(ids),
                "tab": _first(query, "tab", "history")}

    def undo_bulk_dismissal(self) -> dict:
        """Reverse the most recent bulk dismissal.

        Cheap and exact, so it is offered rather than a warning: a dismissal is
        one row in one table, and dismiss_unwatched only ever inserts rows that
        were ABSENT -- it selects undismissed events -- so deleting its batch
        cannot revoke a dismissal made by hand earlier.

        Held in memory, not on disk: this is an undo for the operator who just
        clicked, not an audit trail, and the UI says so rather than offering a
        button that would quietly do nothing after a restart. Only the most
        recent batch, for the same reason.
        """
        with self.local_lock:
            ids, self._undo_batch = self._undo_batch, []
            if ids:
                self.local.executemany(
                    "DELETE FROM acknowledgements WHERE event_id = ?", [(i,) for i in ids])
                self.local.commit()
        return {"restored": len(ids)}

    SEARCH_PER_TYPE = 8

    def _search_releases(self, q: str, limit: int) -> QueryPage:
        """Release matches, one row per build rather than one per region.

        A build ships to many regions, so matching rows directly filled the whole
        dropdown with the same build string repeated -- "S23 Ultra" returned
        S918BXXSAFZH3 eight times, which looks like a broken list and crowds out the
        device and silicon hits. Grouping by build keeps each hit distinct and puts
        the region count in the detail line, where it is information rather than
        repetition. `total` stays the number of matching BUILDS, so the reported
        count and the listed rows are the same unit.
        The FTS5 trigram index narrows which releases the joins below run over.
        The LIKE predicates are UNCHANGED and still decide every match, so the
        answer is the scan's answer -- the index only ever proposes candidates.
        Measured on the live corpus: `SM-S938B` 39.4ms -> 1.1ms, `A15` 36.5ms ->
        0.7ms, `TECNO_W4` 34.8ms -> 0.0ms, and `Galaxy` 43.5ms -> 45.3ms because
        it matches every indexed row and is sent to the scan instead. See
        search_index.py, including what it deliberately does NOT speed up.
        """
        like = like_contains(q)
        narrowing = search_index.plan(self.corpus.connection, q, "firmware_release_id")
        if narrowing.certainly_empty:
            # The index is a superset of the LIKE match, so "no candidate rows"
            # means no row can match and the joins need not run. This is the only
            # place an answer is returned without evaluating the LIKE, and the
            # superset property is the whole of why it is sound.
            return QueryPage([], 0, limit, 0)
        base = ("FROM v_device_region_history WHERE " + narrowing.clause + "(" + " OR ".join(
            like_clause(column) for column in
            ("build_id", "model_code", "variant", "target_code")) + ")")
        params = [*narrowing.params, *([like] * 4)]
        total = self.corpus.connection.execute(
            "SELECT count(DISTINCT build_id) " + base, params).fetchone()[0]
        rows = self.corpus.connection.execute(
            """SELECT build_id build, min(firmware_release_id) id,
                      min(variant) device, min(model_code) model,
                      count(DISTINCT target_code) regions,
                      min(target_code) region """
            + base + " GROUP BY build_id ORDER BY build_id LIMIT ?", [*params, limit]).fetchall()
        return QueryPage([dict(row) for row in rows], total, limit, 0)

    def _search_devices(self, q: str, limit: int) -> QueryPage:
        """Device matches for the search box: identity columns only, no heavy joins.

        Matches the same fields devices_page exposes to `q` (brand, variant, model
        code, codename, and the silicon marketing name/part), so a hit here is a hit
        there -- but without v_latest_firmware, whose whole-corpus window is what
        made the type-ahead cost half a second.
        """
        like = like_contains(q)
        # Reads device_catalog_flat, the same row devices_page reads, which is
        # what keeps the promise above literally true: both now match against
        # the device's primary SoC rather than one matching every part and the
        # other one. It also drops a MATERIALIZE of v_chip_devices per keystroke.
        base = ("FROM device_catalog_flat dc WHERE " + " OR ".join(
            like_clause(column) for column in
            ("dc.brand", "dc.variant", "dc.model_code", "dc.codename",
             "dc.chip_marketing_name", "dc.chip_part_number")))
        params = [like] * 6
        total = self.corpus.connection.execute(
            "SELECT count(DISTINCT dc.hardware_model_id) " + base, params).fetchone()[0]
        rows = self.corpus.connection.execute(
            "SELECT DISTINCT dc.brand maker, dc.variant name, dc.model_code model " + base
            + " ORDER BY dc.variant,dc.model_code LIMIT ?", [*params, limit]).fetchall()
        return QueryPage([dict(row) for row in rows], total, limit, 0)

    def search_payload(self, query: dict[str, list[str]]) -> dict:
        """Global search across the WHOLE catalogue, with honest match counts.

        This used to call self.devices({}) / chips({}) / releases({}) with no
        filter and then filter the result in Python -- which searched only the
        FIRST PAGE of each table and silently reported everything else as absent.
        Measured on the live corpus: 6 of 6 devices sampled from past row 100 were
        invisible, "CAMON 50 Pro 5G" returned nothing, and releases were matched
        against 100 of 21,186 rows. It was a filter over page one wearing the name
        of a search, and it is how a device that IS in the catalogue gets reported
        as missing.

        The query now goes into SQL, so every row is considered. The trade is that
        matching is over each table's declared searchable columns -- identity
        fields: brand, variant, model code, codename, marketing name, part number,
        region, build id -- rather than "any value on the row". That is narrower per
        row and enormously wider per table, and the old any-value matching only ever
        worked for the first page anyway.

        `totals` carries how many rows actually matched, so the caller can say
        "8 of 214" rather than presenting a truncated list as the whole answer.
        """
        q = _first(query, "q", "").strip()
        if not q:
            return {"items": [], "totals": {"device": 0, "chip": 0, "release": 0}, "query": q}

        ask = {"q": [q], "limit": [str(self.SEARCH_PER_TYPE)]}
        # Deliberately NOT devices_page() here. That builds the full Explore row --
        # firmware counts, latest-firmware ranking, silicon, support -- and joins
        # v_latest_firmware, which recomputes a whole-corpus window (~390ms) on every
        # call. A search hit shows a name and a model code, so it reads the catalogue
        # view directly and the type-ahead stops paying for columns it never renders.
        devices = self._search_devices(q, self.SEARCH_PER_TYPE)
        releases = self._search_releases(q, self.SEARCH_PER_TYPE)
        chips = self.chips_page(ask)

        items = (
            [{"type": "device", "id": row["model"], "label": row["name"], "detail": row["model"]}
             for row in devices.items]
            + [{"type": "chip", "id": row["part"], "label": row["name"], "detail": row["part"]}
               for row in chips.items]
            + [{"type": "release", "id": row["id"], "label": row["build"],
                "detail": f"{row['device']} · {row['model']} · "
                          + (f"{row['regions']} regions" if row["regions"] > 1 else str(row["region"]))}
               for row in releases.items]
        )
        return {"items": items,
                "totals": {"device": devices.total, "chip": chips.total, "release": releases.total},
                "query": q}

    def search(self, query: dict[str, list[str]]) -> list[dict]:
        """Back-compat list form. Prefer search_payload(), which reports totals."""
        return self.search_payload(query)["items"]

    def config(self) -> dict:
        defaults = {"cadenceHours": 6, "preferredRegions": ["ILO", "MID", "GLOBAL"],
                    "enabledSources": ["samsung", "xiaomi", "tecno"], "supportedOnly": True}
        with self.local_lock:
            row = self.local.execute("SELECT value_json FROM settings WHERE key='operator_config'").fetchone()
        return defaults if row is None else {**defaults, **json.loads(row[0])}

    def config_options(self) -> dict:
        return {"regions": [{"id": key, "label": value} for key, value in REGION_OPTIONS.items()],
                "sources": [{"id": key, "label": value} for key, value in SOURCE_OPTIONS.items()]}

    def save_config(self, value: dict) -> dict:
        # The same str-is-iterable hole acknowledge-bulk had. Here it happened to
        # land on the right status -- "ILO" becomes ['I','L','O'], no single
        # character is a region key, so the unknown-region check refuses it -- but
        # for the wrong reason, and only because every key in REGION_OPTIONS and
        # SOURCE_OPTIONS is longer than one character. That is a property of the
        # option tables, not of this code, so it is checked rather than relied on.
        regions = [str(x) for x in _require_id_list(
            value.get("preferredRegions", []), "preferredRegions")]
        sources = [str(x) for x in _require_id_list(
            value.get("enabledSources", []), "enabledSources")]
        if any(x not in REGION_OPTIONS for x in regions) or any(x not in SOURCE_OPTIONS for x in sources):
            raise ValueError("unknown region or source")
        clean = {
            "cadenceHours": max(1, min(24, int(value.get("cadenceHours", 6)))),
            "preferredRegions": regions,
            "enabledSources": sources,
            "supportedOnly": bool(value.get("supportedOnly", True)),
        }
        with self.local_lock:
            self.local.execute("INSERT OR REPLACE INTO settings VALUES('operator_config',?)",
                               (json.dumps(clean, sort_keys=True),))
            self.local.commit()
        return clean

    def real_sample(self) -> dict:
        if not self.sample_path or not self.sample_path.is_file():
            return {"items": [], "notice": "No real-source sample installed."}
        return json.loads(self.sample_path.read_text(encoding="utf-8"))

    def review_profiles(self) -> dict:
        root = self.sample_path.parent if self.sample_path else None
        if not root:
            return {"profiles": []}
        profiles: list[dict] = []
        original = root / "review_profiles.json"
        if original.is_file():
            profiles.extend(json.loads(original.read_text(encoding="utf-8")).get("profiles", []))
        samsung = root / "samsung" / "reviewed_profiles.json"
        if samsung.is_file():
            for item in json.loads(samsung.read_text(encoding="utf-8")).get("profiles", []):
                profiles.append({"id": f"samsung:{item['model_code']}", "name": item["commercial_name"],
                    "tier": item["tier"], "source_identity": [item["model_code"], *item.get("regions", [])],
                    "chipset": item.get("chip", {}).get("name", "Not asserted in reviewed profile"),
                    "cpu": None, "gpu": None, "launch_os": f"Android {item.get('android', 'Unknown')}",
                    "display": None, "battery": None, "memory": None, "wifi": None, "bluetooth": None,
                    "spec_observed_at": self.meta["dataAsOf"], "radio": {}, "review_state": item["identity_state"],
                    "firmware": [{"name": region, "codename": item["model_code"], "version": build,
                                  "android": item.get("android"), "date": "captured corpus"}
                                 for region, build in item.get("latest_captured", {}).items()]})
        mixed = root / "xiaomi_tecno_review_profiles.json"
        if mixed.is_file():
            for item in json.loads(mixed.read_text(encoding="utf-8")).get("profiles", []):
                spec = item.get("specification", {})
                profiles.append({"id": item["id"], "name": item["name"], "tier": item["tier"],
                    "source_identity": item.get("source_identities", []), "chipset": spec.get("chipset", "Unknown"),
                    "cpu": spec.get("cpu"), "gpu": spec.get("gpu"), "launch_os": spec.get("launch_os"),
                    "display": spec.get("display"), "battery": spec.get("battery"), "memory": spec.get("memory"),
                    "wifi": spec.get("wifi"), "bluetooth": spec.get("bluetooth"), "spec_observed_at": spec.get("observed_at"),
                    "radio": {"4g": spec.get("4g"), "5g": spec.get("5g")},
                    "review_state": item.get("review_state", "needs identity review"),
                    "firmware": item.get("firmware_history", [])})
        unique = {item["id"]: item for item in profiles}
        return {"profiles": list(unique.values())}

    def watches(self) -> list[dict]:
        with self.local_lock:
            return list_watches(self.local)

    def watchlist(self) -> list[dict]:
        """Every watched subject with its current state, newest first.

        Two subject kinds exist and they live in different layers: a
        hardware_model is canonical, a source_product is evidence whose identity
        is not yet proven. Both are watchable, so both are answered here, and the
        `layer` field says which you are looking at rather than blurring them.

        A subject with no firmware on record returns nulls, which the UI renders
        as a dash. That is the honest outcome for "we follow this and have seen
        nothing yet" and must not be confused with "no updates exist".
        """
        with self.local_lock:
            watches = list_watches(self.local)
        c = self.corpus.connection
        rows: list[dict] = []
        for w in watches:
            kind, sid = w["subject_type"], w["subject_id"]
            item = {"subject_type": kind, "subject_id": sid,
                    "watched_since": w.get("created_at"), "layer": None,
                    "name": None, "maker": None, "model_code": None,
                    "chipset": None, "latest_build": None, "latest_region": None,
                    "latest_android": None, "latest_patch": None,
                    "latest_seen": None, "source": None, "release_count": 0}
            if kind == "hardware_model":
                item["layer"] = "canonical"
                d = c.execute("SELECT * FROM v_device_catalog WHERE hardware_model_id=?",
                              (sid,)).fetchone()
                if d:
                    d = dict(d)
                    item.update(name=d.get("variant") or d.get("model_code"),
                                maker=d.get("brand"), model_code=d.get("model_code"))
                f = c.execute("""SELECT fr.build_id, fr.security_patch_level, fr.baseband_version,
                                        fr.vendor_released_at, fr.last_observed_at, ft.target_code
                                 FROM firmware_releases fr
                                 LEFT JOIN firmware_targets ft ON ft.id=fr.firmware_target_id
                                 WHERE fr.hardware_model_id=?
                                 ORDER BY COALESCE(fr.vendor_released_at, fr.last_observed_at) DESC
                                 LIMIT 1""", (sid,)).fetchone()
                if f:
                    f = dict(f)
                    item.update(latest_build=f.get("build_id"),
                                latest_patch=f.get("security_patch_level"),
                                latest_region=f.get("target_code"),
                                latest_seen=f.get("vendor_released_at") or f.get("last_observed_at"))
                item["release_count"] = c.execute(
                    "SELECT count(*) FROM firmware_releases WHERE hardware_model_id=?",
                    (sid,)).fetchone()[0]
            else:
                item["layer"] = "evidence"
                p = c.execute("SELECT manufacturer, canonical_name, review_state "
                              "FROM source_products WHERE id=?", (sid,)).fetchone()
                if p:
                    p = dict(p)
                    item.update(name=p.get("canonical_name"), maker=p.get("manufacturer"))
                r = c.execute("""SELECT build_id, region_code, android_version, source_id,
                                        vendor_released_at, created_at
                                 FROM product_firmware_releases WHERE product_id=?
                                 ORDER BY COALESCE(vendor_released_at, created_at) DESC
                                 LIMIT 1""", (sid,)).fetchone()
                if r:
                    r = dict(r)
                    item.update(latest_build=r.get("build_id"),
                                latest_region=r.get("region_code"),
                                latest_android=r.get("android_version"),
                                source=r.get("source_id"),
                                latest_seen=r.get("vendor_released_at") or r.get("created_at"))
                item["release_count"] = c.execute(
                    "SELECT count(*) FROM product_firmware_releases WHERE product_id=?",
                    (sid,)).fetchone()[0]
                sil = c.execute("""SELECT part_number FROM observed_product_silicon
                                   WHERE product_id=? LIMIT 1""", (sid,)).fetchone()
                if sil:
                    item["chipset"] = sil[0]
            rows.append(item)
        rows.sort(key=lambda x: (x["latest_seen"] or ""), reverse=True)
        return rows

    def save_watch(self, value: dict) -> dict:
        with self.local_lock:
            return save_watch(self.local, self.corpus.connection, value)

    def identity_decisions(self) -> list[dict]:
        with self.local_lock:
            return [dict(row) for row in self.local.execute(
                "SELECT * FROM identity_decisions ORDER BY decided_at DESC")]

    def save_identity_decision(self, value: dict) -> dict:
        with self.local_lock:
            return save_decision(self.local, value)

    def identity_history(self) -> list[dict]:
        with self.local_lock:
            return [dict(row) for row in self.local.execute("SELECT * FROM identity_decision_history ORDER BY id DESC LIMIT 1000")]

    def agent_proposals(self) -> list[dict]:
        with self.local_lock:
            return list_proposals(self.local)

    def import_agent_proposals(self, values: list) -> dict:
        with self.local_lock:
            return import_proposals(self.local, self.corpus.connection, values)

    def review_agent_proposal(self, identifier: str, value: dict) -> dict:
        with self.local_lock:
            return review_proposal(self.local, identifier, value)

    def collection_requests(self) -> list[dict]:
        with self.local_lock:
            # CollectionWorker's constructor runs CREATE TABLE/ALTER migrations on
            # this connection, so building one per request races itself as well as
            # the reads below.
            worker = CollectionWorker(self.local, self.corpus.connection, self.worker_paths)
            return [worker.get(row[0]) for row in self.local.execute(
                "SELECT id FROM collection_requests ORDER BY id DESC LIMIT 50")]

    def request_collection(self, value: dict) -> dict:
        target = str(value.get("target", "")).strip()
        source = str(value.get("source", "")).strip()
        scope = str(value.get("scope", "latest_firmware")).strip()
        if not target or source not in SOURCE_OPTIONS or scope not in (
                "latest_firmware", "firmware_history", "device_profile", "security"):
            raise ValueError("invalid collection request")
        with self.local_lock:
            cursor = self.local.execute(
                "INSERT INTO collection_requests(target,source,scope) VALUES(?,?,?)",
                (target[:200], source, scope))
            self.local.commit()
        return {"id": cursor.lastrowid, "target": target[:200], "source": source,
                "scope": scope, "status": "queued", "execution_mode": "captured_replay",
                "live_network": False}

    def recover_collection_requests(self) -> dict:
        with self.local_lock:
            return {"interrupted": CollectionWorker(self.local, self.corpus.connection, self.worker_paths).recover_interrupted()}

    def retry_collection_request(self, request_id: int) -> dict:
        with self.local_lock:
            return CollectionWorker(self.local, self.corpus.connection, self.worker_paths).retry(request_id)

    def process_collection_request(self, request_id: int) -> dict:
        with self.local_lock:
            return CollectionWorker(self.local, self.corpus.connection, self.worker_paths).process(request_id)

    def process_next_collection_request(self) -> dict:
        with self.local_lock:
            item = CollectionWorker(self.local, self.corpus.connection, self.worker_paths).process_next()
        return item or {"status": "idle", "message": "No queued collection request."}


def make_handler(service: ObservatoryService, web_root: Path, policy: AccessPolicy,
                 monitor: ExposureMonitor | None = None):
    # `policy` is required, with no default. It defaulted to an open policy so
    # existing callers kept working, which meant any caller that embedded this
    # server and forgot the argument served the whole corpus unauthenticated --
    # and did so silently, which is the property that makes a security default
    # dangerous rather than merely wrong. A caller that genuinely wants no
    # credential says AccessPolicy(None) and is readable as having chosen it.
    #
    # `monitor` DOES get a default, and the asymmetry is deliberate rather than
    # inconsistent: a forgotten policy opens a gate, while a forgotten monitor
    # opens nothing -- the detector still runs, its counters are still readable
    # through this handler's own health route, and only the process-wide startup
    # banner is missing. Defaulting it to None and skipping detection would make
    # "nobody passed one" and "nothing is exposed" the same observation, which is
    # the exact confusion this module exists to end.
    if monitor is None:
        monitor = ExposureMonitor(Posture(
            bind_host="unspecified", port=None,
            token_source="unspecified-by-the-embedding-caller", open=policy.open,
            acknowledged=frozenset()))

    class Handler(BaseHTTPRequestHandler):
        # Largest body any route may declare. The biggest a route actually
        # accepts is the 1 MB proposal import, so this is a ceiling on the
        # DECLARATION -- refused before a single byte is read, rather than after
        # buffering it. An oversized declaration is not an expensive request; it
        # is a request that never becomes one.
        MAX_BODY_BYTES = 2_000_000
        # How long the body may go QUIET, not how long it may take in total.
        # Bounding the total would cut off a legitimately slow large upload;
        # bounding the gap between packets cuts off only a client that has
        # stopped sending -- which is exactly the case Content-Length lied
        # about. See _body().
        BODY_IDLE_TIMEOUT_SECONDS = 5
        # Class-level default so _guarded can read it even if a fault happens
        # before handle_one_request sets it for this request.
        _response_started = False

        def send_response(self, code, message=None) -> None:
            # Recorded so _guarded() knows whether a 500 can still be sent: once
            # a status line is on the wire, a second one would corrupt the
            # response rather than replace it.
            self._response_started = True
            super().send_response(code, message)

        def handle_one_request(self) -> None:
            self._response_started = False
            super().handle_one_request()

        def _body(self) -> bytes:
            """This request's body, or ValueError -- never an unbounded block.

            `self.rfile.read(int(Content-Length))` was written at eight routes,
            and every one of them trusted a header the client controls. The
            header is a CLAIM: read() blocks until that many bytes arrive, so

                curl -m 5 -X POST /api/v1/watches \\
                     -H 'Content-Length: 5000' --data-binary '{}'

            parked the handler thread inside read() -- still holding its
            per-thread SQLite handles -- until the client went away. Measured:
            curl timed out at 5s with no response, and the thread was still
            there. There was no read timeout anywhere in the process, so on any
            interface a stranger can reach, a trickle of these is resource
            exhaustion with no packets of consequence and (before this change)
            no log line either.

            Three refusals, all before or instead of blocking:
            absent/unparseable/negative length, a length above MAX_BODY_BYTES,
            and a body that stops arriving. The caller turns any of them into
            400, because a body that does not match its own header is a bad
            request, not a server fault.

            read1(), not read(): read() on a buffered reader loops internally
            until it has all n bytes, so a timeout around it bounds the TOTAL
            transfer and would cut off a slow honest upload. read1() returns
            whatever one underlying read yields, so the loop below makes the
            timeout bound the GAP between packets instead -- a client that keeps
            sending is never cut off, and one that has stopped is refused in
            BODY_IDLE_TIMEOUT_SECONDS rather than never.

            The socket timeout is set and restored around the read rather than
            once on the connection: it must bound how long we wait for THIS
            body, not how long a legitimately idle keep-alive connection may sit
            between requests.
            """
            raw = self.headers.get("Content-Length")
            if raw is None:
                # No declared body. Not an error -- several routes accept an
                # empty POST -- and notably NOT a reason to read until EOF,
                # which is the other way to block forever.
                return b""
            try:
                length = int(raw)
            except ValueError:
                raise ValueError("Content-Length is not a number") from None
            if length < 0:
                # int() accepts "-1" happily and rfile.read(-1) means READ TO
                # EOF, i.e. block until the client closes. The negative case is
                # therefore the same defect wearing a different number.
                raise ValueError("Content-Length is negative")
            if length > self.MAX_BODY_BYTES:
                raise ValueError(f"declared body of {length} bytes is larger than the "
                                 f"{self.MAX_BODY_BYTES} byte limit")
            # read1 where available (rfile is a BufferedReader in http.server's
            # default configuration); read otherwise, which degrades to a total
            # bound rather than an idle one but never to no bound at all.
            read_some = getattr(self.rfile, "read1", self.rfile.read)
            previous = self.connection.gettimeout()
            self.connection.settimeout(self.BODY_IDLE_TIMEOUT_SECONDS)
            chunks: list[bytes] = []
            received = 0
            try:
                while received < length:
                    chunk = read_some(min(length - received, 65536))
                    if not chunk:
                        break  # clean EOF: the client closed mid-body
                    chunks.append(chunk)
                    received += len(chunk)
            except (TimeoutError, OSError) as exc:
                # A half-read body leaves the connection's framing unknown:
                # whatever arrives next cannot be trusted to be a new request,
                # so this connection does not get reused.
                self.close_connection = True
                raise ValueError(f"body stopped arriving after {received} of {length} bytes "
                                 f"({self.BODY_IDLE_TIMEOUT_SECONDS}s idle)") from exc
            finally:
                self.connection.settimeout(previous)
            if received < length:
                self.close_connection = True
                raise ValueError(f"body is {received} bytes, shorter than the declared {length}")
            return b"".join(chunks)

        def _payload(self, *, limit: int | None = None):
            """The request's JSON body. The ONLY body reader on this handler.

            Eight routes each open-coded `int(Content-Length)` + `rfile.read()`,
            which is how one flaw came to live at eight sites: whichever route a
            probe reached first was the one that looked broken. `limit` is a
            route's own ceiling, tighter than MAX_BODY_BYTES, and is checked
            against the bytes that ACTUALLY arrived rather than against the
            client's claim about them.
            """
            body = self._body()
            if limit is not None and not 0 < len(body) <= limit:
                raise ValueError(f"body must be between 1 and {limit} bytes, not {len(body)}")
            return json.loads(body or b"{}")

        def _guarded(self, route) -> None:
            """Run a route and answer even when it raises.

            An unhandled exception used to propagate into socketserver, which
            printed a traceback and closed the socket: the client saw the
            connection drop with NO HTTP response (curl exit 52), and because
            log_message was a no-op the server kept no record. A fault that
            produces neither a response nor a log line is a fault you find
            twice -- and the first six times it was found here, it was found by
            a crash.

            The exception's own text is NOT sent. An API caller gets a stable
            code; the message goes to the log, where an operator can read it and
            a stranger cannot. See the collection-request routes, where
            `invalid literal for int() with base 10: 'abc'` was being returned
            as API prose.

            It is also where the exposure detector observes every request, for
            the same reason the docstring above do_GET/do_POST gives: this is the
            ONE place both verbs pass through, so a third verb added without
            touching it would be a verb that neither answers safely nor is
            watched. Inside the try on purpose -- a fault in the detector becomes
            a logged 500 like any other, rather than a silently swallowed
            exception in the instrument that is supposed to be telling us the
            truth about our exposure.
            """
            try:
                monitor.observe(
                    headers=self.headers, client_address=self.client_address,
                    # A credential only "travels" if one is configured to travel:
                    # with no token, a Bearer header a client invented is not our
                    # secret and reporting it as leaked would be a false alarm.
                    # An INVALID token still counts -- it is somebody's.
                    credential_presented=(not policy.open) and policy.presented(
                        authorization=self.headers.get("Authorization"),
                        cookie=self.headers.get("Cookie"),
                        query_token=(parse_qs(urlparse(self.path).query).get("token")
                                     or [None])[0]) is not None)
                route()
            except Exception as exc:  # noqa: BLE001 -- the point is to catch everything
                self.log_error("unhandled %s on %s %s: %s",
                               type(exc).__name__, self.command, self.path, exc)
                if not self._response_started:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})
                self.close_connection = True

        def _gate(self, parsed) -> bool:
            """Apply the access policy. True means the request may proceed.

            Called before ANY routing, including static files: a page served to
            an unauthenticated browser is a page that can then be scripted, and
            the whole point of the token on a reachable interface is that the
            box is not a public read-only mirror.
            """
            if policy.open:
                return True
            # Deliberately NOT passing query_token. ?token= is a one-shot
            # bootstrap for a browser that cannot send a header by typing a
            # URL, and _consume_token_param has already handled that case for
            # GET. Accepting it here too authenticated writes from a URL, which
            # puts a permanent credential into proxy logs, monitoring, shared
            # links and browser history -- for no capability a header does not
            # already give.
            decision = policy.decide(
                method=self.command,
                authorization=self.headers.get("Authorization"),
                cookie=self.headers.get("Cookie"),
                origin=self.headers.get("Origin"),
                referer=self.headers.get("Referer"),
                host=self.headers.get("Host"),
                # Behind a TLS-terminating proxy the browser's Origin says
                # https while this process only ever speaks http; comparing
                # them without this rejects every legitimate write.
                secure=(self.headers.get("X-Forwarded-Proto", "").lower() == "https"),
            )
            if decision.allowed:
                return True
            # Not via _json(): that attaches X-Observatory-Data-Mode, which
            # tells an unauthenticated caller whether this is a real snapshot
            # or a demonstration. A refusal should describe the refusal.
            body = json.dumps({"error": decision.reason}, separators=(",", ":")).encode()
            self.send_response(HTTPStatus(decision.status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

        def _consume_token_param(self, parsed) -> bool:
            """Turn ?token=<t> on a GET into a cookie, then redirect without it.

            The operator has a token in a file and a browser that cannot send an
            Authorization header by typing a URL. Redirecting rather than simply
            serving the page is what keeps the credential out of the address bar,
            out of the history, and out of the Referer sent to any link the page
            later points at.
            """
            if policy.open or self.command != "GET":
                return False
            query = parse_qs(parsed.query)
            supplied = (query.get("token") or [None])[0]
            if not supplied or not policy.token_matches(supplied):
                return False
            remaining = {k: v for k, v in query.items() if k != "token"}
            # Collapse every leading slash AND backslash to exactly one slash
            # before using the path as a Location. urlparse leaves "\" alone,
            # but browsers normalise it to "/", so a request for
            #   /\evil.example?token=<valid>
            # produced  Location: /\evil.example , which a browser reads as
            # //evil.example -- a protocol-relative URL, i.e. an off-site
            # redirect. Found by probing this flow rather than by reading it.
            # It only fires for a caller who already holds a valid token, so it
            # was never the way in; it is fixed because a redirector that can be
            # pointed off-site is a building block, and this is three lines.
            safe_path = "/" + parsed.path.lstrip("/\\")
            target = safe_path + (f"?{urlencode(remaining, doseq=True)}" if remaining else "")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", target)
            # No Secure flag: this also serves plain http on an air-gapped box,
            # and setting it there would silently drop the cookie entirely.
            self.send_header("Set-Cookie",
                             f"{COOKIE_NAME}={supplied}; Path=/; HttpOnly; SameSite=Strict")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True

        def handle(self) -> None:
            """Serve this connection, then hand back its corpus connection.

            ThreadingHTTPServer gives every connection its own thread, and the
            corpus hands every thread its own sqlite connection -- that per-thread
            isolation is what stops two requests corrupting one connection's
            statement state. Releasing here, rather than waiting for the thread and
            its locals to be collected, is what keeps open handles proportional to
            requests IN FLIGHT instead of requests EVER SERVED.

            In a finally: a client that disconnects mid-response must not keep a
            handle, and that is the common case under load, not a rare one.
            """
            try:
                super().handle()
            finally:
                service.release_thread_connections()

        # Both verbs go through _guarded, so neither can drop a connection
        # without answering. Adding a third verb means adding it here too.
        def do_GET(self) -> None:  # noqa: N802
            self._guarded(self._route_get)

        def do_POST(self) -> None:  # noqa: N802
            self._guarded(self._route_post)

        def _route_get(self) -> None:
            parsed = urlparse(self.path)
            if self._consume_token_param(parsed):
                return
            if not self._gate(parsed):
                return
            query = parse_qs(parsed.query)
            routes = {
                "/api/v1/radar/overview": service.overview,
                "/api/v1/updates": lambda: _page_payload(service.updates_page(query), service.meta),
                "/api/v1/updates/acknowledgements": lambda: _page_payload(service.acknowledgements_page(query), service.meta),
                # The confirm step's count. A read, so it is not origin-checked;
                # it takes the same query string the POST does.
                "/api/v1/updates/unwatched-pending": lambda: service.unwatched_pending(query),
                "/api/v1/devices": lambda: _page_payload(service.devices_page(query), service.meta),
                "/api/v1/chips/products": lambda: _page_payload(service.chip_products_page(query), service.meta),
                "/api/v1/chips": lambda: _page_payload(service.chips_page(query), service.meta),
                "/api/v1/releases": lambda: _page_payload(service.releases_page(query), service.meta),
                "/api/v1/product-releases": lambda: _page_payload(service.product_releases_page(query), service.meta),
                "/api/v1/product-security": lambda: _page_payload(service.product_security_page(query), service.meta),
                "/api/v1/product-source-builds": lambda: _page_payload(service.product_source_builds_page(query), service.meta),
                "/api/v1/source-records": lambda: _page_payload(service.source_records_page(query), service.meta),
                "/api/v1/identity/products": lambda: _page_payload(service.source_products_page(query), service.meta),
                "/api/v1/security/findings": lambda: _page_payload(service.security_page(query), service.meta),
                "/api/v1/security/coverage": service.security_coverage,
                # `posture` is here and not only in the startup log because the
                # two exposures it reports are invisible to the process that
                # caused them: whoever put the proxy in front is not the person
                # tailing this server's stderr. A monitor can gate on
                # posture.alarming alone.
                "/api/v1/admin/health": lambda: {"items": service.health(), "meta": service.meta,
                                                 "posture": monitor.report(),
                                                 **service.integrity()},
                "/api/v1/search": lambda: {**service.search_payload(query), "meta": service.meta},
                "/api/v1/admin/config": service.config,
                "/api/v1/admin/options": service.config_options,
                "/api/v1/admin/real-sample": service.real_sample,
                "/api/v1/admin/review-profiles": service.review_profiles,
                "/api/v1/identity/history": lambda: {"items": service.identity_history()},
                "/api/v1/identity/agent-proposals": lambda: {"items": service.agent_proposals()},
                "/api/v1/watches": lambda: {"items": service.watches()},
                "/api/v1/watchlist": lambda: {"items": service.watchlist()},
                "/api/v1/identity/decisions": lambda: {"items": service.identity_decisions()},
                "/api/v1/identity/agent-bundle": service.agent_review_bundle,
                "/api/v1/identity/agent-bundle/prompt": service.agent_review_prompt,
                "/api/v1/admin/collection-requests": lambda: {"items": service.collection_requests()},
            }
            if parsed.path.startswith('/api/v1/security/cves/'):
                try:
                    self._json(HTTPStatus.OK, service.security_detail(unquote(parsed.path[len('/api/v1/security/cves/'):])) )
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {'error': 'cve_not_found'})
                return
            if parsed.path.startswith('/api/v1/devices/'):
                try:
                    self._json(HTTPStatus.OK, service.device_detail(unquote(parsed.path[len('/api/v1/devices/'):])) )
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {'error': 'device_not_found'})
                return
            if parsed.path.startswith('/api/v1/products/'):
                try:
                    self._json(HTTPStatus.OK, service.product_detail(unquote(parsed.path[len('/api/v1/products/'):])) )
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {'error': 'product_not_found'})
                return
            if parsed.path in routes:
                self._json(HTTPStatus.OK, routes[parsed.path]())
                return
            self._static(parsed.path)

        def _route_post(self) -> None:
            parsed = urlparse(self.path)
            if not self._gate(parsed):
                return
            if parsed.path == "/api/v1/updates/acknowledge-bulk":
                try:
                    payload = self._payload()
                    self._json(HTTPStatus.OK, service.acknowledge_many(payload.get("ids", [])))
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "update_not_found"})
                except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                    # AttributeError too: a body of `[]` or `"x"` has no .get,
                    # and a payload that is the wrong SHAPE is a bad request,
                    # not the 500 it used to become.
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_update_ids"})
                return
            # Filters ride the query string, not the body, so the confirm step's
            # GET and this POST are the same URL with two methods -- there is no
            # second representation of "which rows" to drift out of step.
            if parsed.path == "/api/v1/updates/dismiss-unwatched":
                self._json(HTTPStatus.OK, service.dismiss_unwatched(parse_qs(parsed.query)))
                return
            if parsed.path == "/api/v1/updates/dismiss-unwatched/undo":
                self._json(HTTPStatus.OK, service.undo_bulk_dismissal())
                return
            if parsed.path == "/api/v1/admin/config":
                try:
                    self._json(HTTPStatus.OK, service.save_config(self._payload()))
                except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_config"})
                return
            if parsed.path == "/api/v1/identity/agent-proposals":
                try:
                    self._json(HTTPStatus.CREATED,
                               service.import_agent_proposals(self._payload(limit=1_000_000)))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST,
                               {"error": "invalid_agent_proposals", "detail": _refusal(exc)})
                return
            proposal_prefix = "/api/v1/identity/agent-proposals/"
            if parsed.path.startswith(proposal_prefix) and parsed.path.endswith("/review"):
                try:
                    self._json(HTTPStatus.OK, service.review_agent_proposal(
                        unquote(parsed.path[len(proposal_prefix):-len("/review")]),
                        self._payload(limit=20_000)))
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "proposal_not_found"})
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST,
                               {"error": "invalid_proposal_review", "detail": _refusal(exc)})
                return
            if parsed.path == "/api/v1/watches":
                try:
                    self._json(HTTPStatus.OK, service.save_watch(self._payload()))
                except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_watch"})
                return
            if parsed.path == "/api/v1/identity/decisions":
                try:
                    self._json(HTTPStatus.OK, service.save_identity_decision(self._payload()))
                except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_identity_decision"})
                return
            if parsed.path == "/api/v1/admin/collection-requests":
                try:
                    self._json(HTTPStatus.CREATED, service.request_collection(self._payload()))
                except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_collection_request"})
                return
            if parsed.path == "/api/v1/admin/collection-requests/process-next":
                try:
                    self._json(HTTPStatus.OK, service.process_next_collection_request())
                except (ValueError, OSError) as exc:
                    self._json(HTTPStatus.CONFLICT,
                               {"error": "collection_failed", "detail": _refusal(exc)})
                return
            if parsed.path == "/api/v1/admin/collection-requests/recover":
                try:
                    self._json(HTTPStatus.OK, service.recover_collection_requests())
                except (ValueError, OSError) as exc:
                    self._json(HTTPStatus.CONFLICT,
                               {"error": "worker_active", "detail": _refusal(exc)})
                return
            # Both collection-request id routes, sharing one parse. A request id
            # is an INTEGER primary key, so `/abc/retry` names no request that
            # could exist -- exactly what every other malformed-id path in this
            # server answers 404 to. It used to let int() raise inside the try,
            # which produced `409 {"detail": "invalid literal for int() with
            # base 10: 'abc'"}`: the wrong status class (409 asserts a conflict
            # with some real state, and there was none) carrying a raw Python
            # exception message as API prose.
            request_prefix = "/api/v1/admin/collection-requests/"
            for suffix, action, ok, conflict in (
                    ("/retry", service.retry_collection_request,
                     HTTPStatus.CREATED, "retry_conflict"),
                    ("/process", service.process_collection_request,
                     HTTPStatus.OK, "collection_request_conflict")):
                if not (parsed.path.startswith(request_prefix) and parsed.path.endswith(suffix)):
                    continue
                raw_id = parsed.path[len(request_prefix):-len(suffix)]
                try:
                    request_id = int(raw_id)
                except ValueError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "collection_request_not_found"})
                    return
                if not SQLITE_MIN_INT <= request_id <= SQLITE_MAX_INT:
                    # The third member of the query-string integer family, found
                    # by the malformed-request sweep rather than by inspection:
                    # `/api/v1/admin/collection-requests/<2**63>/retry` parses as
                    # an int, so the ValueError guard above lets it through, and
                    # it then raises OverflowError at bind time inside the worker.
                    # An id SQLite cannot store is an id no row can have, so this
                    # is a 404 for the same reason 'abc' is.
                    self._json(HTTPStatus.NOT_FOUND, {"error": "collection_request_not_found"})
                    return
                try:
                    self._json(ok, action(request_id))
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "collection_request_not_found"})
                except (ValueError, OSError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": conflict, "detail": _refusal(exc)})
                return
            product_prefix = "/api/v1/identity/products/"
            if parsed.path.startswith(product_prefix) and parsed.path.endswith("/review"):
                product_id = unquote(parsed.path[len(product_prefix):-len("/review")])
                try:
                    payload = self._payload()
                    self._json(HTTPStatus.OK,
                               service.review_source_product(product_id, payload.get("decision", "")))
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "product_not_found"})
                except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_product_decision"})
                return
            prefix, suffix = "/api/v1/updates/", "/acknowledge"
            if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
                event_id = unquote(parsed.path[len(prefix):-len(suffix)])
                try:
                    service.acknowledge(event_id)
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "update_not_found"})
                    return
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def _json(self, status: HTTPStatus, payload) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Observatory-Data-Mode", service.meta["mode"])
            self.end_headers()
            self.wfile.write(body)

        def _static(self, raw_path: str) -> None:
            # An unrecognised /api/ path used to fall through to here and get the
            # SPA shell back with a 200. A monitoring probe pointed at a typo'd or
            # renamed route would then pass forever, which is worse than no probe
            # at all: it reports health it never checked. Only the UI's own routes
            # may fall through to index.html.
            if raw_path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "unknown_endpoint", "path": raw_path})
                return
            relative = "index.html" if raw_path == "/" else raw_path.lstrip("/")
            target = (web_root / relative).resolve()
            if web_root.resolve() not in target.parents and target != web_root.resolve():
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not target.is_file():
                target = web_root / "index.html"
            body = target.read_bytes()
            # A validator, so a reload does not re-send the frontend. Every load
            # fetched all of it: index.html + styles.css + app.js + api.js =
            # 192,230 bytes, with no ETag, no Last-Modified and no Cache-Control,
            # so the browser had nothing to revalidate against and no choice but to
            # ask again. Measured twice in one browser context: 192,230 bytes both
            # times.
            #
            # Strong ETag over the bytes just read rather than an mtime: a checkout,
            # a redeploy or a `cp -a` moves mtimes without changing content, and
            # this is a tool people run from a fresh clone. Hashing 192KB per
            # request is ~0.1ms and only happens for the four frontend files.
            #
            # no-cache, NOT a max-age: the file is the application, an operator
            # editing app.js expects a reload to show it, and a max-age would serve
            # a stale UI against a corpus that had moved on. no-cache means "always
            # revalidate", so the 304 path is taken whenever the bytes are unchanged
            # and the new bytes are taken the moment they are not.
            etag = '"%s"' % hashlib.sha256(body).hexdigest()[:32]
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            """One line per request and per error, on stderr.

            This was `return` -- a deliberate silence, from when the server had
            one operator on loopback and the lines were noise. The cost only
            showed when a request FAILED: an unhandled exception dropped the
            connection and left nothing behind at all, so the only way to learn
            it had happened was to reproduce it. Six of the defects in this
            round were found that way. A fault that leaves no log entry is a
            fault you find twice.

            The stdlib calls this from log_request (status and size) and from
            log_error (the reason), so restoring it covers both with one method.

            Control characters are stripped because the request line and the
            path come off the wire: without that, a caller chooses what a
            terminal renders and can forge whole log lines with \\r\\n. flush
            because stdout/stderr to a pipe is block-buffered -- the lesson
            already paid for by the startup banner vanishing under nohup.
            """
            line = (format % args) if args else format
            safe = "".join(ch if ch.isprintable() else "\\x%02x" % ord(ch) for ch in line)
            print(f"{self.address_string()} - - [{self.log_date_time_string()}] {safe}",
                  file=sys.stderr, flush=True)

    return Handler


def _refusal(exc: BaseException) -> str:
    """The `detail` text an API caller may be shown for a refusal.

    Every `raise ValueError` behind these routes carries a sentence this
    codebase WROTE for an operator to read ("Only failed, partial or interrupted
    requests can be retried."), so a ValueError's message is relayed.

    An OSError's is not. It carries strerror plus the filesystem PATH it failed
    on -- a server-side detail, handed to anyone who can reach the port, and the
    handoff is explicit that a loopback bind behind a proxy or tunnel is still
    remotely reachable.

    A TypeError's is not either. Nothing behind these routes raises TypeError
    deliberately -- `grep -rn 'raise TypeError' src/` finds only
    _require_id_list, which no detail-carrying route calls -- so any TypeError
    here came from the interpreter and describes our code, not the request.

    Both go to the log instead, where an operator can read them and a stranger
    cannot.

    This is the second half of the fix, not the whole one: a filter cannot tell
    an authored ValueError from an accidental one, and the raw message that was
    actually reaching clients came from int(). That one is fixed where it
    belongs -- by parsing the id before dispatching, so the exception is never
    raised. tests/test_error_bodies_are_authored.py sweeps the malformed-request
    matrix and fails on a raw-interpreter or filesystem-path fingerprint in any
    response body.
    """
    if isinstance(exc, (OSError, TypeError)):
        return "the server could not complete this request; see the server log"
    return str(exc)


def _first(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def _require_id_list(value, what: str) -> list:
    """A JSON field that must be an ARRAY, checked before anything iterates it.

    `isinstance(value, str)` first is the whole point: a str satisfies every
    duck-typed "is it iterable" test and then yields characters, so a
    client-supplied string silently became a list of one-character ids. A bare
    `iter(value)` guard would have accepted it; so would `hasattr(value,
    '__iter__')`. Only an explicit type check refuses it.

    Also refuses dict and bytes -- a dict iterates its keys, bytes its integers,
    and both would produce a plausible-looking list of the wrong thing. The
    accepted types are list and tuple, which is what json.loads yields for an
    array.
    """
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{what} must be a JSON array, not {type(value).__name__}")
    return list(value)


# SQLite binds integers as signed 64-bit. Python's are arbitrary precision, so
# int("9223372036854775808") succeeds, max()/min() succeed, and the value only
# fails at BIND time -- inside the query, after the route has committed to
# answering -- with `OverflowError: Python int too large to convert to SQLite
# INTEGER`. OverflowError is not a ValueError, so nothing caught it: the handler
# thread died, socketserver closed the socket, and the client got no HTTP
# response at all (measured: curl exit 52, empty body). Every paginating route
# shared it.
SQLITE_MAX_INT = 2 ** 63 - 1
SQLITE_MIN_INT = -(2 ** 63)


def _query_int(raw: str, *, default: int, low: int, high: int) -> int:
    """A query-string integer SQLite can always bind.

    Two failure modes, one function, because they were being handled in
    different places and neither was handled everywhere:

    - not a number at all -> `default` (a mistyped query string must not be an
      error page);
    - a number outside SQLite's signed 64-bit range -> clamped into it.

    Clamping rather than rejecting, deliberately: `offset=10**20` means "past
    the end" and an empty page IS that answer, while `limit=10**20` means "as
    many as you allow" and the cap already answers it. Neither is a request the
    server cannot understand, so neither earns a 4xx -- but both must arrive at
    the driver as a bindable value.

    `low`/`high` are always inside the SQLite range, which is what makes the
    clamp total rather than best-effort.
    """
    assert SQLITE_MIN_INT <= low <= high <= SQLITE_MAX_INT, "bounds must be bindable"
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _pagination(query: dict[str, list[str]]) -> tuple[int, int]:
    # Cap raised from 200 to 500 so the rows-per-page control can offer a page
    # big enough to scan without paging. Still capped: an uncapped limit turns
    # one mistyped query string into a full-table render.
    #
    # Fixed HERE and only here, for all twelve callers. The same bug had already
    # been found at six routes; patching those six would have left the other six
    # for the next probe, which is exactly how the last one-bug-five-sites
    # episode in this codebase played out. tests/test_hostile_query_strings.py
    # scans the source for `_pagination(` call sites and fails if one appears in
    # a function the sweep does not cover.
    limit = _query_int(_first(query, "limit", "100"), default=100, low=1, high=500)
    raw_offset = _first(query, "cursor", _first(query, "offset", "0"))
    offset = _query_int(raw_offset, default=0, low=0, high=SQLITE_MAX_INT)
    return limit, offset


def _sql_filters(query: dict[str, list[str]], fields: dict[str, str],
                 searchable: tuple[str, ...]) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    q = _first(query, "q").strip()
    if q:
        clauses.append("(" + " OR ".join(like_clause(column) for column in searchable) + ")")
        params.extend([like_contains(q)] * len(searchable))
    for key, column in fields.items():
        value = _first(query, key).strip()
        if value:
            clauses.append(like_clause(column))
            params.append(like_contains(value))
    return clauses, params


def _page_payload(page: QueryPage, meta: dict) -> dict:
    return {"items": page.items, "meta": {**meta, "page": page.metadata()},
            "coverage": {"status": "partial", "reason": "snapshot_scope"},
            "data_as_of": meta["dataAsOf"]}


# _filter() lived here: an in-Python row matcher applied to an ALREADY PAGED result.
# It was removed rather than left unused. Its only caller was search(), where it
# filtered the first page of each table and reported everything beyond it as absent
# -- a device that exists reading as a device that does not. Filtering belongs in
# SQL, where it sees every row; a helper that filters a page will eventually be
# reused on another page. If you need matching, add the column to that query's
# searchable tuple in _sql_filters.


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Mobile Observatory locally")
    parser.add_argument("--demo", action="store_true", help="seed and serve explicitly synthetic fixtures")
    parser.add_argument("--data-dir", default=".observatory-data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--legacy-root", help="captured legacy artifact root used by the manual replay worker")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = data_dir / "corpus.sqlite"
    corpus = Database(corpus_path, check_same_thread=False)
    # EVERY startup WRITE below happens inside the BATCH LOCK, and nothing used
    # to. `batch.py` takes `<data-dir>/batch.lock` around its whole run; the
    # server never referenced it, while doing two writes of its own --
    # apply_migrations() and a projection rebuild. Started during the nightly
    # window, which a systemd restart policy will eventually do, that is two
    # writers on one corpus.sqlite.
    #
    # Nonblocking, and a refusal rather than a wait: the condition clears by
    # itself in minutes, a service manager will restart, and a server that waits
    # silently is indistinguishable from one that hung.
    schema_before = corpus.schema_version()
    pending = corpus.pending_migrations()
    try:
        with exclusive_worker(data_dir / "batch.lock",
                              holder="This server's startup migration"):
            if pending:
                # SAID BEFORE IT HAPPENS, because afterwards there is nothing to
                # say it about. This moves somebody's corpus forward with no
                # backup and no undo: the packaged bundle sits at schema 8 and
                # was being carried 25 versions on first launch, and the live
                # corpus reached 33 this way while the handoff still said a human
                # had to apply 0033. An operator who learns of it from a schema
                # number afterwards was never told.
                print(f"applying {len(pending)} migration(s) to {corpus_path}: schema "
                      f"{schema_before} -> {pending[-1]} ({', '.join(f'{v:04d}' for v in pending)}). "
                      f"This changes the database in place and takes no backup. "
                      f"Copy {data_dir} first if you need the version you have.",
                      flush=True)
            corpus.apply_migrations()
            if args.demo:
                seed_demonstration(corpus, root / "fixtures" / "supported_catalog.sample.json")
            elif corpus.connection.execute(
                    "SELECT count(*) FROM sources").fetchone()[0] == 0:
                parser.error("empty corpus: pass --demo for synthetic data or provide "
                             "an ingested corpus")
            # Applying the migrations above CREATES the serving projections empty. On a
            # corpus upgraded in place that means /devices and search would answer with
            # an empty catalogue -- confidently, and with a 200 -- until someone happened
            # to run a batch. That is the same shape of falsehood this whole read path
            # was rebuilt to remove, so it is built here rather than served hollow.
            if not args.demo:
                catalogue = corpus.connection.execute(
                    "SELECT count(*) FROM hardware_models").fetchone()[0]
                published = corpus.connection.execute(
                    "SELECT count(*) FROM device_catalog_flat").fetchone()[0]
                if catalogue and published != catalogue:
                    print(f"projection covers {published} of {catalogue} devices; "
                          f"rebuilding before serving", flush=True)
                    try:
                        report = build_current_firmware(corpus)
                        print(f"  {report.summary()}", flush=True)
                    except ProjectionError as error:
                        parser.error(
                            f"the device catalogue cannot be served: {error}. "
                            "Run `PYTHONPATH=src python3 -m mobile_observatory.batch` to "
                            "rebuild it; refusing to start rather than report an empty "
                            "catalogue as the answer.")
    except ValueError as exc:
        # exclusive_worker raises ValueError when the lock is already held.
        parser.error(
            f"{exc} This server WRITES at startup -- it applies migrations and may "
            f"rebuild the device projection -- and two writers on one corpus.sqlite is "
            f"how a half-written database happens. Start it again once the batch "
            f"finishes.")
    service = ObservatoryService(corpus, data_dir / "local.sqlite", demonstration=args.demo,
                                 sample_path=root / "fixtures" / "real_source_sample.json",
                                 legacy_root=args.legacy_root)
    # Resolve the access token against the interface we are ABOUT to bind, not
    # against what is configured: binding somewhere reachable with no credential
    # is the case that must not pass quietly, and it is decided by --host alone.
    # BEFORE token_for_binding, which is what mints a file. 'none' here and a
    # token afterwards is the only way to tell "this deployment chose a
    # credential" from "this process generated one because nobody had".
    provenance = token_provenance(data_dir)
    token, note = token_for_binding(data_dir, args.host)
    policy = AccessPolicy(token)
    if token is not None and provenance == "none":
        provenance = "minted file"
    posture = posture_at_startup(data_dir=data_dir, host=args.host, port=args.port,
                                 token=token, token_source=provenance,
                                 schema_version=corpus.schema_version(),
                                 migrations_applied=pending)
    monitor = ExposureMonitor(posture)
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(service, root / "apps" / "web", policy, monitor))
    print(f"Mobile Observatory: http://{args.host}:{args.port} ({service.meta['mode']})", flush=True)
    # `note` names the token's FILE and never its value -- printing the value
    # would put a live credential into terminal scrollback and journald.
    #
    # flush=True on every startup print, because stdout to a pipe is block
    # buffered: under nohup, systemd or any service manager that does not pass
    # -u, these lines sit in the buffer until the process exits. Measured: the
    # whole startup banner was absent from a backgrounded run's log. For this
    # line that is not cosmetic -- it is the only thing telling an operator
    # WHERE the generated token is, and it would be swallowed exactly when it
    # is needed. run-batch.sh already passes -u for the same reason.
    print(f"  access: {note}", flush=True)
    # One line stating the effective posture, so an operator reading a startup
    # log does not have to reconstruct it from three other lines and a guess.
    # Goes to stderr with the alarms it belongs beside -- see posture.py.
    monitor.announce()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.local.close()
        corpus.close()


if __name__ == "__main__":
    main()
