"""Dismissing every unwatched item must clear only what the operator can see.

The feature is one click that writes thousands of acknowledgement rows, so the
two ways it can be wrong are both silent:

- it dismisses a WATCHED subject, and the queue the operator built to follow a
  device quietly loses it;
- it dismisses rows OUTSIDE the tab and filters on screen, so a search box with
  "Xiaomi" in it clears 5,700 Samsung events the operator never looked at.

Both come from the same root cause -- a second copy of "which events is the
operator looking at" -- so the predicate lives in ObservatoryService's
_event_feed_query/_event_watch_clause helpers and both the feed and the
dismissal go through them. These tests hold that property rather than the
implementation: the preview count, the rows that disappear and the rows that
stay are all compared against updates_page()'s own answer.

The live-socket class at the bottom exists because this is a state-changing POST
and a live CSRF was demonstrated against this app once already. A policy that
returns the right answer while do_POST never calls it is the shape of bug that
must not survive, and it is only visible over a real socket.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory import Database  # noqa: E402
from mobile_observatory.access import AccessPolicy  # noqa: E402
from mobile_observatory.repository import CanonicalRepository, Event  # noqa: E402
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService, make_handler  # noqa: E402

# A fixture value, never a deployed credential.
TOKEN = "fixture-token-not-a-real-credential"


def build_corpus(data_dir: Path) -> tuple[Database, ObservatoryService]:
    """A demonstration corpus with radar events on every seeded release.

    The seed gives two hardware models with different brands (Samsung, Xiaomi),
    which is what makes "watched stays, unwatched goes" and "the maker filter
    bounds it" distinguishable at all.
    """
    corpus = Database.migrated(data_dir / "corpus.sqlite", check_same_thread=False)
    seed_demonstration(corpus, ROOT / "fixtures" / "supported_catalog.sample.json")
    repository = CanonicalRepository(corpus)
    releases = corpus.connection.execute(
        "SELECT id, hardware_model_id FROM firmware_releases ORDER BY id").fetchall()
    for release in releases:
        for n in range(3):
            repository.append_event(Event(
                "firmware_replaced", "firmware_release", release["id"],
                f"dismiss-{release['id']}-{n}", "2024-01-01T00:00:00Z",
                {"build": "old", "android": 14}, {"build": f"new-{n}", "android": 15}, None))
    service = ObservatoryService(corpus, data_dir / "local.sqlite", demonstration=True)
    return corpus, service


class DismissUnwatchedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.corpus, self.service = build_corpus(Path(self._temp.name))
        self.addCleanup(self.corpus.close)
        self.addCleanup(self.service.local.close)
        self.samsung, self.xiaomi = (
            self.corpus.connection.execute(
                "SELECT hardware_model_id FROM v_device_catalog WHERE brand=?",
                (brand,)).fetchone()[0] for brand in ("Samsung", "Xiaomi"))

    # -- helpers ---------------------------------------------------------------
    def ids_for(self, **query) -> set[str]:
        """Every event id the feed would show for this view, across all pages."""
        page = self.service.updates_page({**{k: [v] for k, v in query.items()},
                                          "limit": ["500"]})
        self.assertLessEqual(page.total, 500, "precondition: one page holds the fixture")
        return {row["id"] for row in page.items}

    def dismissed(self) -> set[str]:
        return set(self.service.acknowledgements())

    def watch(self, subject_id: str) -> None:
        self.service.save_watch({"subjectType": "hardware_model",
                                 "subjectId": subject_id, "enabled": True})

    # -- the feature -----------------------------------------------------------
    def test_watched_subjects_stay_in_the_queue_and_the_unwatched_go(self) -> None:
        self.watch(self.samsung)
        watched_rows = self.ids_for(tab="watched")
        everything = self.ids_for(tab="history")
        self.assertTrue(watched_rows and watched_rows < everything,
                        "precondition: some but not all events are watched")

        result = self.service.dismiss_unwatched({"tab": ["history"]})

        self.assertEqual(everything - watched_rows, self.dismissed())
        self.assertEqual(len(everything - watched_rows), result["dismissed"])
        self.assertEqual(watched_rows, self.ids_for(tab="new"),
                         "the watched rows -- and only those -- must remain in New")

    def test_the_current_filters_bound_the_dismissal(self) -> None:
        """Somebody with a filter applied is looking at a subset and means it.

        Dismissing the whole corpus instead is the surprise that cannot be taken
        back by re-typing the filter.
        """
        xiaomi_rows = self.ids_for(tab="history", maker="Xiaomi")
        everything = self.ids_for(tab="history")
        self.assertTrue(xiaomi_rows and xiaomi_rows < everything,
                        "precondition: the maker filter selects a strict subset")

        result = self.service.dismiss_unwatched({"tab": ["history"], "maker": ["Xiaomi"]})

        self.assertEqual(xiaomi_rows, self.dismissed())
        self.assertEqual(len(xiaomi_rows), result["dismissed"])
        self.assertEqual(everything - xiaomi_rows, self.ids_for(tab="new"))

    def test_the_count_shown_before_the_click_is_the_number_dismissed(self) -> None:
        """The confirm step states a number, so that number must be the effect
        and not a second estimate computed a different way."""
        self.watch(self.samsung)
        whole_tab = self.service.unwatched_pending({"tab": ["history"]})["count"]
        # A filter that happens to select EVERY unwatched row makes this test
        # vacuous: a preview computed for the whole tab would agree with the
        # effect anyway. Caught by planting exactly that defect and watching the
        # first version of this test pass. `q` narrows to one of the three
        # events on the same release, which no other filter in this fixture can.
        narrowing = {"tab": ["history"], "q": ["new-1"]}
        self.assertLess(self.service.unwatched_pending(narrowing)["count"], whole_tab,
                        "precondition: this filter selects a STRICT subset of the unwatched rows")
        for query in ({"tab": ["history"]}, narrowing, {"tab": ["new"], "maker": ["Xiaomi"]},
                      {"tab": ["history"], "region": ["GLOBAL"]}):
            with self.subTest(query=query):
                preview = self.service.unwatched_pending(query)
                self.assertGreater(preview["count"], 0,
                                   "precondition: this view has rows to dismiss")
                self.assertEqual(preview["count"],
                                 self.service.dismiss_unwatched(query)["dismissed"])
                # Undo doubles as the reset, so each view is measured from the
                # same starting point rather than from the previous one's leftovers.
                self.service.undo_bulk_dismissal()
                self.assertEqual(set(), self.dismissed())

    def test_the_preview_names_the_filters_it_is_bounded_by(self) -> None:
        """The UI has to say plainly WHAT is about to happen, which it cannot do
        from a bare count."""
        preview = self.service.unwatched_pending(
            {"tab": ["new"], "maker": ["Xiaomi"], "change": ["Android upgrade"]})
        self.assertEqual("new", preview["tab"])
        self.assertEqual(["maker", "change"], preview["filters"])
        self.assertEqual([], self.service.unwatched_pending({"tab": ["history"]})["filters"],
                         "an unfiltered view must not claim a filter is applied")

    def test_a_second_dismissal_reports_zero_rather_than_re_dismissing(self) -> None:
        first = self.service.dismiss_unwatched({"tab": ["history"]})
        self.assertGreater(first["dismissed"], 0)
        self.assertEqual(0, self.service.dismiss_unwatched({"tab": ["history"]})["dismissed"])
        self.assertEqual(0, self.service.unwatched_pending({"tab": ["history"]})["count"])

    def test_the_watched_tab_dismisses_nothing(self) -> None:
        """Every row there is watched by construction. Reporting a count of zero
        is the honest answer; dismissing them would contradict the tab."""
        self.watch(self.samsung)
        before = self.ids_for(tab="watched")
        self.assertTrue(before, "precondition: the Watched tab has rows")
        self.assertEqual(0, self.service.unwatched_pending({"tab": ["watched"]})["count"])
        self.assertEqual(0, self.service.dismiss_unwatched({"tab": ["watched"]})["dismissed"])
        self.assertEqual(set(), self.dismissed())

    def test_an_event_belonging_to_neither_layer_is_still_unwatched(self) -> None:
        """`NULL IN (...)` is NULL, so negating the watch test without coalesce
        drops an event whose subject resolves to neither a firmware release nor a
        source product -- an unwatched row silently surviving "dismiss all
        unwatched", which is the failure this feature exists to avoid.
        """
        CanonicalRepository(self.corpus).append_event(Event(
            "firmware_replaced", "firmware_release", "no-such-release-id",
            "dismiss-orphan", "2024-01-01T00:00:00Z", {"build": "old"}, {"build": "new"}, None))
        orphan = self.corpus.connection.execute(
            "SELECT id FROM domain_events WHERE dedupe_key='dismiss-orphan'").fetchone()[0]
        self.watch(self.samsung)
        self.assertIn(orphan, self.ids_for(tab="history"),
                      "precondition: the orphan event is in the feed")

        self.service.dismiss_unwatched({"tab": ["history"]})

        self.assertIn(orphan, self.dismissed())

    # -- undo ------------------------------------------------------------------
    def test_undo_restores_the_batch_and_leaves_an_earlier_dismissal_alone(self) -> None:
        """Undo is offered because it is exact, not merely cheap: the batch only
        ever contains rows that were absent, so it cannot revoke a dismissal the
        operator made by hand earlier."""
        everything = sorted(self.ids_for(tab="history"))
        by_hand = everything[0]
        self.service.acknowledge(by_hand)

        dismissal = self.service.dismiss_unwatched({"tab": ["history"]})
        self.assertEqual(len(everything) - 1, dismissal["dismissed"])

        restored = self.service.undo_bulk_dismissal()
        self.assertEqual(len(everything) - 1, restored["restored"])
        self.assertEqual({by_hand}, self.dismissed(),
                         "undo must not revoke the acknowledgement made by hand")
        self.assertEqual(0, self.service.undo_bulk_dismissal()["restored"],
                         "undo is not repeatable; there is one batch")

    def test_a_dismissal_larger_than_the_request_body_cap_is_not_refused(self) -> None:
        """The 5,000-id cap guards an untrusted request BODY. A freshly ingested
        corpus legitimately has more radar events than that (5,869 here), and
        refusing to clear them would make the feature useless exactly when it is
        most needed. The cap is lowered rather than the fixture grown so the
        property is checked in milliseconds."""
        self.service.BULK_ACKNOWLEDGE_LIMIT = 2
        ids = sorted(self.ids_for(tab="history"))
        self.assertGreater(len(ids), 2, "precondition: the fixture exceeds the lowered cap")
        with self.assertRaises(ValueError):
            self.service.acknowledge_many(ids)
        self.assertEqual(len(ids), self.service.dismiss_unwatched({"tab": ["history"]})["dismissed"])

    def test_the_feed_and_the_dismissal_read_one_predicate(self) -> None:
        """The property, stated directly: for every view, what is dismissed is
        exactly what the feed showed minus the watched and the already-dismissed.
        A second copy of the filter logic fails here before it can ship."""
        self.watch(self.xiaomi)
        views = [{}, {"tab": ["new"]}, {"tab": ["history"]},
                 {"tab": ["history"], "maker": ["Samsung"]},
                 {"tab": ["history"], "q": ["SM-S931B"]},
                 {"tab": ["history"], "change": ["Android upgrade"]},
                 {"tab": ["history"], "region": ["nothing-matches-this"]}]
        selective = 0
        for view in views:
            with self.subTest(view=view):
                shown = {row["id"] for row in
                         self.service.updates_page({**view, "limit": ["500"]}).items}
                already = self.dismissed()
                expected = {i for i in shown if i not in already} - self.ids_for(tab="watched")
                selective += bool(expected)
                self.assertEqual(expected,
                                 set(self.service._unwatched_pending_ids(view)))
        # An agreement between two empty sets agrees about nothing. The last view
        # deliberately matches no row; the rest must not.
        self.assertEqual(len(views) - 1, selective,
                         "precondition: these views select rows, so equality means something")


class DismissUnwatchedOverHttpTests(unittest.TestCase):
    """A real socket with a real token policy. This is a state-changing POST."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.corpus, self.service = build_corpus(Path(self._temp.name))
        self.addCleanup(self.corpus.close)
        self.addCleanup(self.service.local.close)

        handler = make_handler(self.service, ROOT / "apps" / "web", AccessPolicy(TOKEN))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)

    def call(self, path, *, method="GET", headers=None, body=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=body.encode() if body else None, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def dismissed_count(self) -> int:
        return self.service.local.execute("SELECT count(*) FROM acknowledgements").fetchone()[0]

    ROUTES = ("/api/v1/updates/dismiss-unwatched?tab=history",
              "/api/v1/updates/dismiss-unwatched/undo")

    def test_the_csrf_attack_that_worked_before_is_refused_on_these_routes(self) -> None:
        """text/plain + a hostile Origin: the CORS-simple cross-site write that
        was demonstrated against POST /api/v1/watches and returned 200."""
        for path in self.ROUTES:
            with self.subTest(path=path):
                before = self.dismissed_count()
                status, _ = self.call(path, method="POST",
                                      headers={"Content-Type": "text/plain",
                                               "Origin": "https://evil.example",
                                               "Authorization": f"Bearer {TOKEN}"},
                                      body="{}")
                self.assertEqual(403, status)
                self.assertEqual(before, self.dismissed_count(),
                                 "a refused bulk dismissal must not have landed")

    def test_an_unauthenticated_bulk_dismissal_is_refused(self) -> None:
        for path in self.ROUTES:
            with self.subTest(path=path):
                before = self.dismissed_count()
                status, _ = self.call(path, method="POST",
                                      headers={"Content-Type": "application/json"}, body="{}")
                self.assertEqual(401, status)
                self.assertEqual(before, self.dismissed_count())

    def test_the_preview_read_also_needs_the_token(self) -> None:
        path = "/api/v1/updates/unwatched-pending?tab=history"
        self.assertEqual(401, self.call(path)[0])
        status, body = self.call(path, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(200, status)
        self.assertGreater(json.loads(body)["count"], 0)

    def test_an_authenticated_same_origin_dismissal_and_undo_work(self) -> None:
        """The control must not simply break the product: a gate that breaks the
        feature gets turned off."""
        headers = {"Content-Type": "application/json",
                   "Origin": f"http://127.0.0.1:{self.port}",
                   "Host": f"127.0.0.1:{self.port}",
                   "Authorization": f"Bearer {TOKEN}"}
        status, body = self.call("/api/v1/updates/dismiss-unwatched?tab=history",
                                 method="POST", headers=headers, body="{}")
        self.assertEqual(200, status)
        dismissed = json.loads(body)["dismissed"]
        self.assertGreater(dismissed, 0)
        self.assertEqual(dismissed, self.dismissed_count())

        status, body = self.call("/api/v1/updates/dismiss-unwatched/undo",
                                 method="POST", headers=headers, body="{}")
        self.assertEqual(200, status)
        self.assertEqual(dismissed, json.loads(body)["restored"])
        self.assertEqual(0, self.dismissed_count())

    def test_the_page_offset_does_not_bound_the_dismissal(self) -> None:
        """The scope is the tab and the filters, never the page you happen to be
        standing on. A limit that silently bounded the effect would leave the
        queue half-cleared and report a number the operator could not explain."""
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {TOKEN}"}
        total = self.service.updates_page({"tab": ["history"], "limit": ["500"]}).total
        self.assertGreater(total, 2, "precondition: more events than the limit below")
        status, body = self.call(
            "/api/v1/updates/dismiss-unwatched?tab=history&limit=1&offset=2",
            method="POST", headers=headers, body="{}")
        self.assertEqual(200, status)
        self.assertEqual(total, json.loads(body)["dismissed"])


if __name__ == "__main__":
    unittest.main()
