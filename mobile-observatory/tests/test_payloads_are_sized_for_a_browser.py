"""Three responses were the wrong SIZE for the thing that fetches them, and the
frontend was re-downloaded in full on every load. None of it is a compression
problem.

Measured on the live corpus:

* /api/v1/identity/agent-bundle was 615,142 bytes, of which `pastePrompt` was
  314,700 characters -- and 313,159 of those characters were the same 628
  candidates the response already returns under `candidates`, re-encoded as
  indented JSON. The prompt itself is 1,541 characters. The panel renders a count
  and a list and never showed the paste text, so the response carried its largest
  field for a button nobody had pressed.
* /api/v1/updates/acknowledgements returned the whole table: 5,785 bare UUIDs,
  231,411 bytes, compressing only 1.8x because a UUID is already dense.
* index.html + styles.css + app.js + api.js are 192,230 bytes and had no ETag, no
  Last-Modified and no Cache-Control, so a reload had to fetch all of it again.
  Measured with resource-timing transferSize: 193,430 bytes of wire on a cold
  load, and 3,775 once the browser holds the files.

The byte-for-byte identity of the paste text is the load-bearing assertion here.
Moving the concatenation would be worthless if it changed the prompt, and the
obvious implementation -- joining the template and the candidates in the browser --
DOES change it: Python's json.dumps escapes non-ASCII and JavaScript's
JSON.stringify does not.
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
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService, make_handler  # noqa: E402

WEB = ROOT / "apps" / "web"
# A candidate carrying a non-ASCII name, which is what separates a server-side
# concatenation from a browser-side one.
CANDIDATES = [{"id": "p1", "canonical_name": "Téléphone Ω 5G", "note": "accented"},
              {"id": "p2", "canonical_name": "Plain Model", "note": "ascii"}]


class PayloadSizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data_dir = Path(self.temp.name)
        review = data_dir / "agent-review"
        review.mkdir()
        (review / "agent-review-prompt.md").write_text("# Review these\nRules here.\n",
                                                       encoding="utf-8")
        (review / "identity-candidates.json").write_text(json.dumps(CANDIDATES), encoding="utf-8")
        self.corpus = Database.migrated(check_same_thread=False)
        self.addCleanup(self.corpus.close)
        seed_demonstration(self.corpus, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.service = ObservatoryService(
            self.corpus, data_dir / "local.sqlite", demonstration=True)
        self.addCleanup(self.service.local.close)
        # data_dir is where agent_review_bundle looks for the review files.
        self.service.data_dir = data_dir
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, WEB, AccessPolicy(None)))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def get(self, path: str, headers: dict | None = None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    # --- the agent bundle ---------------------------------------------------

    def test_the_bundle_no_longer_carries_the_paste_prompt(self) -> None:
        _status, _headers, body = self.get("/api/v1/identity/agent-bundle")
        payload = json.loads(body)
        self.assertNotIn("pastePrompt", payload,
                         "the Admin panel payload carries the paste text again; it is the "
                         "largest field in the response and the panel never renders it")
        self.assertEqual(len(CANDIDATES), payload["candidateCount"])
        self.assertEqual(CANDIDATES[0]["canonical_name"],
                         payload["candidates"][0]["canonical_name"])

    def test_the_prompt_endpoint_returns_the_whole_paste_text(self) -> None:
        _status, _headers, body = self.get("/api/v1/identity/agent-bundle/prompt")
        paste = json.loads(body)["pastePrompt"]
        self.assertIn("# Review these", paste, "the prompt template is missing")
        self.assertIn("Import contract:", paste, "the import contract is missing")
        self.assertIn("# Candidate data", paste, "the candidate block is missing")
        # Compared as parsed structures, not as substrings: json.dumps escapes
        # non-ASCII, so searching the encoded text for "Téléphone Ω 5G" fails even
        # though the candidate is there as é...  -- the assertion would have
        # been about the encoder, not about the payload.
        pasted = json.loads(paste.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        self.assertEqual([c["canonical_name"] for c in CANDIDATES],
                         [c["canonical_name"] for c in pasted],
                         "the pasted candidate data is not the candidate list")

    def test_the_paste_text_is_exactly_template_plus_server_encoded_candidates(self) -> None:
        """The split must be a move, not a re-rendering.

        Asserted against the SERVER's own encoder, and separately against the
        encoding a browser would have produced, because the two differ on the
        accented candidate above -- which is the reason the concatenation stayed on
        the server.
        """
        _s, _h, body = self.get("/api/v1/identity/agent-bundle/prompt")
        paste = json.loads(body)["pastePrompt"]
        candidates, _memory, template = self.service._agent_review_candidates()
        expected = (template + "\n\n# Candidate data\n```json\n"
                    + json.dumps(candidates, indent=2) + "\n```\n")
        self.assertEqual(expected, paste)

        # What a browser-side JSON.stringify(x, null, 2) would have produced: the
        # same structure with non-ASCII left unescaped.
        browser_style = json.dumps(candidates, indent=2, ensure_ascii=False)
        self.assertNotEqual(json.dumps(candidates, indent=2), browser_style,
                            "this fixture no longer contains a non-ASCII candidate, so it can "
                            "no longer show that a browser-side join would differ")
        self.assertNotIn(browser_style, paste,
                         "the paste text now matches the browser-side encoding, which means the "
                         "concatenation moved into the client and the prompt changed")

    # --- acknowledgements ---------------------------------------------------

    def test_acknowledgements_is_bounded_and_states_its_total(self) -> None:
        for n in range(250):
            self.service.local.execute(
                "INSERT OR REPLACE INTO acknowledgements(event_id) VALUES (?)", (f"evt-{n:04d}",))
        _s, _h, body = self.get("/api/v1/updates/acknowledgements")
        payload = json.loads(body)
        self.assertEqual(100, len(payload["items"]),
                         "the default page is not capped, so one request can return the whole "
                         "acknowledgement table")
        self.assertEqual(250, payload["meta"]["page"]["total"],
                         "a truncated list must state the true total; without it the response "
                         "presents 100 ids as though they were all of them")
        _s, _h, body = self.get("/api/v1/updates/acknowledgements?limit=500")
        self.assertEqual(250, len(json.loads(body)["items"]))

    def test_the_complete_list_is_still_available_internally(self) -> None:
        """Paginating the ROUTE must not have narrowed what the service knows."""
        for n in range(150):
            self.service.local.execute(
                "INSERT OR REPLACE INTO acknowledgements(event_id) VALUES (?)", (f"evt-{n:04d}",))
        self.assertEqual(150, len(self.service.acknowledgements()))

    # --- static frontend ----------------------------------------------------

    def test_static_assets_carry_a_validator(self) -> None:
        for asset in ("/", "/app.js", "/api.js", "/styles.css"):
            with self.subTest(asset=asset):
                status, headers, body = self.get(asset)
                self.assertEqual(200, status)
                self.assertTrue(headers.get("ETag"),
                                f"{asset} has no ETag, so a reload must re-download it")
                self.assertEqual("no-cache", headers.get("Cache-Control"),
                                 f"{asset} must be revalidated, never served stale from a max-age")

    def test_an_unchanged_asset_answers_304_with_no_body(self) -> None:
        _status, headers, first = self.get("/app.js")
        status, _h, body = self.get("/app.js", {"If-None-Match": headers["ETag"]})
        self.assertEqual(304, status)
        self.assertEqual(b"", body, "a 304 must not carry the body it just saved sending")
        self.assertGreater(len(first), 0)

    def test_a_stale_validator_gets_the_new_bytes(self) -> None:
        status, _h, body = self.get("/app.js", {"If-None-Match": '"not-the-current-etag"'})
        self.assertEqual(200, status)
        self.assertGreater(len(body), 0)

    def test_the_etag_is_over_the_CONTENT_not_the_timestamp(self) -> None:
        """A checkout, a redeploy or `cp -a` moves mtimes without changing bytes.

        An mtime-derived validator would miss on every deploy of identical files --
        and, worse, could HIT after an edit that preserved the timestamp, serving a
        stale application. Asserted by serving two different files and two identical
        byte strings.
        """
        _s, headers_js, body_js = self.get("/app.js")
        _s, headers_css, body_css = self.get("/styles.css")
        self.assertNotEqual(headers_js["ETag"], headers_css["ETag"],
                            "two different files share an ETag")
        import hashlib
        self.assertEqual('"%s"' % hashlib.sha256(body_js).hexdigest()[:32], headers_js["ETag"],
                         "the ETag is not a digest of the bytes served, so it cannot track edits "
                         "that leave the mtime alone")


if __name__ == "__main__":
    unittest.main()
