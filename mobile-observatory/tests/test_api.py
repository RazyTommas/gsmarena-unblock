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
from mobile_observatory.seed import seed_demonstration  # noqa: E402
from mobile_observatory.server import ObservatoryService, make_handler  # noqa: E402
from mobile_observatory.repository import CanonicalRepository, Event  # noqa: E402


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.corpus = Database.migrated(check_same_thread=False)
        seed_demonstration(self.corpus, ROOT / "fixtures" / "supported_catalog.sample.json")
        self.service = ObservatoryService(
            self.corpus, Path(self.temp.name) / "local.sqlite", demonstration=True,
            sample_path=ROOT / "fixtures" / "real_source_sample.json"
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, ROOT / "apps" / "web")
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.service.local.close()
        self.corpus.close()
        self.temp.cleanup()

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path) as response:
            return response, json.load(response)

    def test_frontend_endpoints_and_demo_label(self) -> None:
        for path in (
            "/api/v1/radar/overview",
            "/api/v1/updates",
            "/api/v1/devices",
            "/api/v1/chips",
            "/api/v1/releases",
            "/api/v1/product-releases",
            "/api/v1/product-security",
            "/api/v1/security/findings",
            "/api/v1/admin/health",
            "/api/v1/search?q=Samsung",
            "/api/v1/admin/config",
            "/api/v1/admin/options",
            "/api/v1/admin/real-sample",
            "/api/v1/admin/review-profiles",
            "/api/v1/identity/decisions",
        ):
            response, payload = self.get(path)
            self.assertEqual(response.headers["X-Observatory-Data-Mode"], "demonstration")
            if path not in ("/api/v1/admin/config", "/api/v1/admin/options",
                            "/api/v1/admin/real-sample", "/api/v1/admin/review-profiles",
                            "/api/v1/identity/decisions"):
                self.assertIn("DEMONSTRATION", json.dumps(payload))

    def test_devices_are_canonical_seed_not_frontend_fixture(self) -> None:
        _, payload = self.get("/api/v1/devices?q=SM-S931B")
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["model"], "SM-S931B")
        self.assertEqual(payload["items"][0]["confidence"], "Demonstration")

    def test_acknowledgement_is_local_and_changes_unseen(self) -> None:
        _, before = self.get("/api/v1/radar/overview")
        _, updates = self.get("/api/v1/updates")
        event_id = updates["items"][0]["id"]
        request = urllib.request.Request(
            f"{self.base}/api/v1/updates/{event_id}/acknowledge", method="POST", data=b""
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 204)
        _, after = self.get("/api/v1/radar/overview")
        self.assertEqual(after["unseen"], before["unseen"] - 1)

    def test_unknown_update_cannot_be_acknowledged(self) -> None:
        request = urllib.request.Request(
            f"{self.base}/api/v1/updates/not-real/acknowledge", method="POST", data=b""
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 404)

    def test_bulk_acknowledgement_is_atomic_and_persistent(self) -> None:
        _, before = self.get("/api/v1/radar/overview")
        _, updates = self.get("/api/v1/updates")
        ids = [item["id"] for item in updates["items"][:2]]
        request = urllib.request.Request(
            f"{self.base}/api/v1/updates/acknowledge-bulk", method="POST",
            data=json.dumps({"ids": ids}).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        self.assertEqual(result["acknowledged"], len(ids))
        _, after = self.get("/api/v1/radar/overview")
        self.assertEqual(after["unseen"], before["unseen"] - len(ids))

    def test_security_findings_are_newest_first_and_explain_reasoning(self) -> None:
        _, payload = self.get("/api/v1/security/findings?limit=100")
        dates = [row["published_at"] for row in payload["items"] if row["published_at"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertTrue(all(row["reasoning"] for row in payload["items"]))
        self.assertTrue(all(row["cve_url"].endswith(row["cve"]) for row in payload["items"]))

    def test_cve_detail_route_returns_provenance_and_missing_is_404(self) -> None:
        c = self.corpus.connection
        source = c.execute('SELECT id FROM sources LIMIT 1').fetchone()[0]
        evidence = c.execute('SELECT id FROM evidence LIMIT 1').fetchone()[0]
        c.execute("INSERT INTO advisories VALUES('api-advisory',?,'test','Test bulletin',NULL,NULL,?)", (source,evidence))
        c.execute("INSERT INTO vulnerabilities VALUES('api-cve','CVE-2099-10001',NULL,NULL,NULL)")
        c.execute("INSERT INTO advisory_vulnerabilities VALUES('api-advisory','api-cve')")
        _, payload = self.get("/api/v1/security/findings?limit=1")
        cve = payload['items'][0]['cve']
        _, detail = self.get('/api/v1/security/cves/' + cve)
        self.assertEqual(detail['cve'], cve)
        self.assertTrue(detail['bulletins'])
        self.assertIn('boundaries', detail)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get('/api/v1/security/cves/CVE-2099-999999')
        self.assertEqual(error.exception.code, 404)

    def test_silicon_defaults_to_mobile_linked_parts_and_keeps_evidence_levels(self) -> None:
        _, payload = self.get("/api/v1/chips?limit=100")
        rows = payload["items"]
        linked = [row for row in rows if row["devices"]]
        unlinked = [row for row in rows if not row["devices"]]
        if linked and unlinked:
            self.assertLess(rows.index(linked[-1]), rows.index(unlinked[0]))
        self.assertTrue(all(row["devices"] == row["canonical_devices"] + row["product_devices"]
                            for row in rows))

    def test_silicon_empty_late_page_keeps_total(self) -> None:
        _, first = self.get('/api/v1/chips?limit=1')
        _, late = self.get('/api/v1/chips?offset=99999')
        self.assertEqual(late['items'], [])
        self.assertEqual(late['meta']['page']['total'], first['meta']['page']['total'])

    def test_manual_collection_request_is_executed_as_truthful_captured_replay(self) -> None:
        body = json.dumps({"target": "SM-S938B / ILO", "source": "samsung",
                           "scope": "latest_firmware"}).encode()
        request = urllib.request.Request(
            f"{self.base}/api/v1/admin/collection-requests", method="POST", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            queued = json.load(response)
        process = urllib.request.Request(
            f"{self.base}/api/v1/admin/collection-requests/{queued['id']}/process",
            method="POST", data=b"")
        with urllib.request.urlopen(process) as response:
            finished = json.load(response)
        self.assertIn(finished["status"], ("succeeded", "partial"))
        self.assertEqual(finished["execution_mode"], "captured_replay")
        self.assertFalse(finished["live_network"])
        self.assertFalse(finished["result"]["liveNetwork"])
        self.assertGreaterEqual(finished["result"]["accepted"], 1)
        self.assertTrue(finished["logs"])

    def test_unavailable_live_collector_fails_with_auditable_result(self) -> None:
        body = json.dumps({"target": "Pixel 10", "source": "android", "scope": "security"}).encode()
        request = urllib.request.Request(
            f"{self.base}/api/v1/admin/collection-requests", method="POST", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            queued = json.load(response)
        process = urllib.request.Request(
            f"{self.base}/api/v1/admin/collection-requests/{queued['id']}/process",
            method="POST", data=b"")
        with urllib.request.urlopen(process) as response:
            finished = json.load(response)
        self.assertEqual(finished["status"], "failed")
        self.assertIn("live collector unavailable", finished["result"]["error"])

    def test_operator_config_round_trip(self) -> None:
        body = json.dumps({"cadenceHours": 12, "preferredRegions": ["ILO"],
                           "enabledSources": ["samsung"], "supportedOnly": True}).encode()
        request = urllib.request.Request(f"{self.base}/api/v1/admin/config", method="POST",
                                         data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            saved = json.load(response)
        self.assertEqual(saved["cadenceHours"], 12)
        _, loaded = self.get("/api/v1/admin/config")
        self.assertEqual(loaded, saved)

    def test_release_history_and_search(self) -> None:
        _, releases = self.get("/api/v1/releases?q=S931B")
        self.assertEqual(len(releases["items"]), 2)
        _, search = self.get("/api/v1/search?q=SAMPLE-S931B")
        self.assertTrue(any(item["type"] == "release" for item in search["items"]))

    def test_server_side_filter_and_cursor_pagination(self) -> None:
        _, first = self.get("/api/v1/devices?vendor=Qualcomm&limit=2")
        self.assertEqual(len(first["items"]), 2)
        self.assertGreaterEqual(first["meta"]["page"]["total"], 3)
        self.assertEqual(first["meta"]["page"]["nextCursor"], "2")
        _, second = self.get("/api/v1/devices?vendor=Qualcomm&limit=2&cursor=2")
        self.assertNotEqual({x["model"] for x in first["items"]},
                            {x["model"] for x in second["items"]})

    def test_release_filters_execute_against_history_read_model(self) -> None:
        _, payload = self.get("/api/v1/releases?model=SM-S931B&region=ILO&limit=1")
        self.assertEqual(payload["meta"]["page"]["total"], 1)
        self.assertEqual(payload["items"][0]["region"], "ILO")
        self.assertEqual(payload["coverage"]["status"], "partial")

    def test_canonical_roms_are_newest_effective_date_first(self) -> None:
        _, payload = self.get("/api/v1/releases?limit=20")
        effective = [item["released"] or item["observed"] for item in payload["items"]]
        self.assertEqual(effective, sorted(effective, reverse=True))

    def test_devices_report_firmware_coverage_without_inventing_history(self) -> None:
        _, observed = self.get("/api/v1/devices?model=SM-S931B")
        self.assertGreater(observed["items"][0]["firmware_count"], 0)
        self.assertEqual(observed["items"][0]["firmwareCoverage"], "observed")
        _, absent = self.get("/api/v1/devices?model=SM-A566B")
        self.assertEqual(absent["items"][0]["firmware_count"], 0)
        self.assertEqual(absent["items"][0]["firmwareCoverage"], "not_observed")

    def test_android_upgrade_metric_and_radar_label_require_before_after_evidence(self) -> None:
        release_id = self.corpus.connection.execute(
            "SELECT id FROM firmware_releases LIMIT 1").fetchone()[0]
        CanonicalRepository(self.corpus).append_event(Event(
            "firmware_replaced", "firmware_release", release_id, "test:android:upgrade",
            "2026-09-15T00:00:00Z", {"android": 15, "build": "old"},
            {"android": 16, "build": "new"}, None))
        _, overview = self.get("/api/v1/radar/overview")
        self.assertEqual(overview["androidUpgrades"], 1)
        _, updates = self.get("/api/v1/updates?q=new")
        self.assertEqual(updates["items"][0]["change"], "Android upgrade")
        self.assertEqual(updates["items"][0]["importance"], "high")

    def test_query_read_models_are_installed(self) -> None:
        views = {row[0] for row in self.corpus.connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='view'")}
        self.assertTrue({"v_latest_firmware", "v_device_region_history", "v_chip_devices"} <= views)

    def test_identity_decision_is_durable_local_memory(self) -> None:
        body = json.dumps({"sourceNamespace": "xiaomi", "sourceValue": "dada_global",
                           "canonicalType": "hardware_model", "canonicalId": "model-1",
                           "decision": "different"}).encode()
        request = urllib.request.Request(f"{self.base}/api/v1/identity/decisions", method="POST",
                                         data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(json.load(response)["decision"], "different")
        _, remembered = self.get("/api/v1/identity/decisions")
        self.assertEqual(remembered["items"][0]["source_value"], "dada_global")

    def test_device_detail_pages_all_exact_model_history(self) -> None:
        c = self.corpus.connection
        hardware = c.execute('SELECT hardware_model_id,model_code FROM v_device_catalog LIMIT 1').fetchone()
        hardware_id, model = hardware
        target = c.execute('SELECT id,target_code FROM firmware_targets LIMIT 1').fetchone()
        now = '2026-01-01T00:00:00Z'
        initial = c.execute('SELECT count(*) FROM firmware_releases WHERE hardware_model_id=?', (hardware_id,)).fetchone()[0]
        for n in range(235):
            c.execute("""INSERT INTO firmware_releases(id,hardware_model_id,firmware_target_id,build_id,channel,
                first_observed_at,last_observed_at,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (f'device-history-{n}',hardware_id,target[0],f'HISTORY{n}','stable' if n%2 else 'beta',now,now,now))
        _, detail = self.get('/api/v1/devices/' + model)
        self.assertEqual(detail['device']['model_code'], model)
        self.assertEqual(detail['firmware']['meta']['page']['total'], initial+235)
        self.assertEqual(len(detail['firmware']['items']),50)
        seen=set(); cursor='0'
        while cursor is not None:
            _, page=self.get('/api/v1/releases?model_exact='+model+'&limit=50&cursor='+cursor)
            ids={row['id'] for row in page['items']}
            self.assertFalse(seen & ids)
            seen |= ids
            self.assertTrue(all(row['model']==model for row in page['items']))
            cursor=page['meta']['page']['nextCursor']
        self.assertEqual(len(seen),initial+235)
        _, beta=self.get('/api/v1/releases?model_exact='+model+'&channel_exact=beta&region_exact='+target[1])
        self.assertEqual(beta['meta']['page']['total'],118)
        _, empty=self.get('/api/v1/releases?model_exact='+model[:-1])
        self.assertEqual(empty['meta']['page']['total'],0)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get('/api/v1/devices/' + model[:-1])
        self.assertEqual(error.exception.code,404)

    def test_product_detail_and_reverse_silicon_preserve_full_paged_history(self) -> None:
        c = self.corpus.connection
        now = '2026-01-01T00:00:00Z'
        c.execute("INSERT INTO sources VALUES('products-test','Product test',NULL,'secondary',1,?)", (now,))
        c.execute("""INSERT INTO ingestion_runs(id,source_id,started_at,outcome,parser_name,parser_version)
            VALUES('products-run','products-test',?,'succeeded','test','1')""", (now,))
        c.execute("INSERT INTO artifacts VALUES('products-artifact','products-test','products-run',?,'text/csv','https://example.test/source',?,'local.csv',1)", ('a'*64,now))
        for pid in ('p-one','p-two'):
            c.execute('INSERT INTO source_products VALUES(?,?,?,?,?,NULL,?,?)', (pid,'Xiaomi',pid,pid,'approved',now,now))
            c.execute('INSERT INTO source_identity_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (pid,'products-test','codename',pid,pid,pid,'approved','test','1','high',now,now))
        proof=[{'source':'gsmarena_captured_specs','source_url':'https://www.gsmarena.com/test.php','slug':'test.php'}]
        c.execute('INSERT INTO observed_product_silicon VALUES(?,?,?,?,?,?,?,?)',
                  ('p-one','Qualcomm Test','Qualcomm','Test','Test',json.dumps(proof),'high',now))
        c.execute('INSERT INTO observed_product_silicon VALUES(?,?,?,?,?,?,?,?)',
                  ('p-two','Qualcomm Test Plus','Qualcomm','Test Plus','Test Plus',json.dumps(proof),'high',now))
        for n in range(235):
            oid=f'product-observation-{n}'
            c.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,NULL)',
                (oid,'products-test','products-run','products-artifact','firmware_release',oid,now,
                 json.dumps({'data':{'download_url':f'https://example.test/rom-{n}.zip'}}),'b'*64,'valid'))
            c.execute('INSERT INTO product_firmware_releases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (oid,'p-one','p-one',oid,'products-test','GLOBAL' if n%2 else 'EEA',f'build-{n}',
                 'Stable',None,None,now,'Recovery',now))
        for n,pid in enumerate(('p-one','p-two')):
            c.execute('INSERT INTO product_security_publications VALUES(?,?,?,?,?,?,?,?,?)',
                (pid,pid,pid,f'product-observation-{n}','products-test','2026-01',now,'Publication',now))
        _, detail = self.get('/api/v1/products/p-one')
        self.assertEqual(detail['coverage']['hardware'],'not_established')
        self.assertEqual(detail['firmware']['meta']['page']['total'],235)
        self.assertEqual(len(detail['firmware']['items']),50)
        self.assertEqual(detail['security']['meta']['page']['total'],1)
        self.assertEqual(detail['silicon']['evidence'],proof)
        self.assertEqual(detail['lastObserved'],now)
        seen=set(); cursor='0'
        while cursor is not None:
            _, page=self.get('/api/v1/product-releases?product=p-one&limit=50&cursor='+cursor)
            ids={r['id'] for r in page['items']}
            self.assertFalse(seen & ids)
            seen |= ids
            self.assertTrue(all(r['android'] is None and r['download_url'] and r['source_url'] for r in page['items']))
            cursor=page['meta']['page']['nextCursor']
        self.assertEqual(len(seen),235)
        _, filtered=self.get('/api/v1/product-releases?product=p-one&region=EEA&channel=Stable')
        self.assertEqual(filtered['meta']['page']['total'],118)
        _, reverse=self.get('/api/v1/chips/products?vendor=Qualcomm&part=Test')
        self.assertEqual([r['id'] for r in reverse['items']],['p-one'])
        self.assertEqual(reverse['items'][0]['evidence'],proof)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get('/api/v1/products/missing')
        self.assertEqual(error.exception.code,404)


if __name__ == "__main__":
    unittest.main()
