from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobile_observatory.database import Database  # noqa: E402
from mobile_observatory.enrichment import (automate_identity_review, import_mediatek_catalog,
                                            import_security_catalog, write_agent_review_bundle)  # noqa: E402


class EnrichmentTests(unittest.TestCase):
    def test_security_import_preserves_evidence_and_only_exact_part_applicability(self) -> None:
        db = Database.migrated()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asb = root / "asb.csv"
            with asb.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["bulletin_month", "cve", "tier", "section", "component"])
                writer.writeheader(); writer.writerow({"bulletin_month": "2026-01", "cve": "CVE-2026-1000",
                    "tier": "05", "section": "Qualcomm", "component": "WLAN"})
            mediatek = root / "mediatek.csv"
            with mediatek.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["cve", "severity", "component", "chipsets", "bulletin_month"])
                writer.writeheader(); writer.writerow({"cve": "CVE-2026-2000", "severity": "High",
                    "component": "Modem", "chipsets": "MT6789 and vague family", "bulletin_month": "2026-02"})
            asb_result = import_security_catalog(db.connection, asb)
            mt_result = import_mediatek_catalog(db.connection, mediatek)
            self.assertEqual(asb_result["spl_fix_claims"], 1)
            self.assertEqual(asb_result["component_claims"], 1)
            self.assertEqual(mt_result["silicon_parts"], 1)
            self.assertEqual(mt_result["part_applicability_claims"], 1)
            claim = db.connection.execute("""SELECT sp.part_number,e.locator FROM applicability_claims ac
              JOIN silicon_parts sp ON sp.id=ac.subject_id JOIN evidence e ON e.id=ac.evidence_id
              WHERE ac.subject_type='silicon_part'""").fetchone()
            self.assertEqual((claim["part_number"], claim["locator"]), ("MT6789", "csv:line=2"))
            gap = db.connection.execute("SELECT status FROM security_coverage_gaps WHERE vendor='Qualcomm'").fetchone()
            self.assertEqual(gap["status"], "missing")
        db.close()

    def test_exact_evidence_approves_and_unknown_stays_explicit(self) -> None:
        db = Database.migrated()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            devices = root / "devices.yml"
            devices.write_text("ruby:\n- Redmi Note 12 Turbo China\n- RUBY\n", encoding="utf-8")
            specs = root / "specs.csv"
            with specs.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["device_name", "slug", "chipset", "fetched_at"])
                writer.writeheader(); writer.writerow({"device_name": "Xiaomi Redmi Note 12 Turbo",
                    "slug": "xiaomi_redmi_note_12_turbo", "chipset": "Qualcomm Snapdragon 7+ Gen 2 (4 nm)",
                    "fetched_at": "2026-01-01T00:00:00Z"})
            play = root / "play.csv"
            with play.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["Retail Branding", "Marketing Name", "Device", "Model"])
                writer.writeheader(); writer.writerow({"Retail Branding": "Tecno", "Marketing Name": "CAMON 17 Pro",
                    "Device": "TECNO-CG8", "Model": "TECNO CG8"})
            now = "2026-01-01T00:00:00Z"
            with db.connection:
                db.connection.execute("INSERT INTO sources VALUES('x','X',NULL,'primary',1,?)", (now,))
                for pid, maker, name, identity in (("p1", "Xiaomi", "Redmi Note 12 Turbo", "ruby"),
                                                   ("p2", "TECNO", "CAMON 17 Pro", "CAMON 17 Pro"),
                                                   ("p3", "TECNO", "Mystery", "Mystery")):
                    db.connection.execute("INSERT INTO source_products VALUES(?,?,?,?,?,?,?,?)",
                                          (pid, maker, name, name.casefold(), "proposed", None, now, now))
                    db.connection.execute("INSERT INTO source_identity_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                          ("i" + pid, "x", "source", identity, identity.casefold(), pid,
                                           "proposed", "test", "1", "low", now, now))
            result = automate_identity_review(db.connection, devices_yml=devices, specs_csv=specs, google_play_csv=play)
            self.assertEqual(result["total"], 3)
            self.assertEqual(result["auto_approved"], 2)
            self.assertEqual(result["insufficient_evidence"], 1)
            self.assertEqual(result["silicon_observations"], 1)
            bundle = write_agent_review_bundle(db.connection, root / "bundle")
            self.assertEqual(bundle["candidate_count"], 1)
            self.assertTrue((root / "bundle" / "agent-review-prompt.md").is_file())
        db.close()


if __name__ == "__main__":
    unittest.main()
