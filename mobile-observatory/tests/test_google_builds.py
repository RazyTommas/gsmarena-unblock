from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).parents[1]/'src'))
from mobile_observatory.database import Database
from mobile_observatory.google_builds import URL,import_google_builds,parse_builds,product_source_builds

HTML='''<table><tr><th>Build ID</th><th>Tag</th><th>Version</th><th>Supported devices</th><th>Security patch level</th></tr>
<tr><td>BUILD15</td><td>android-15.0.0_r1</td><td>Android15</td><td>Pixel 9, Pixel 9 Pro</td><td>2025-05-05</td></tr>
<tr><td>BUILD17</td><td>android-17.0.0_r1</td><td>Android17</td><td></td><td>2026-06-05</td></tr></table>'''


class GoogleSourceBuildTests(unittest.TestCase):
    def capture(self,root):
        raw=root/'builds.html';raw.write_text(HTML)
        manifest=root/'capture.json';manifest.write_text(json.dumps(dict(format_version=1,source_url=URL,
            raw_path=raw.name,sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),observed_at='2026-09-17T00:00:00Z')))
        return manifest

    def test_empty_device_lists_do_not_imply_pixel_android_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);db=Database.migrated(root/'corpus.sqlite');c=db.connection
            try:
                path=self.capture(root);totals=import_google_builds(c,path)
                self.assertEqual(totals['unassigned_builds'],1)
                self.assertEqual(totals['pixel_product_links'],2)
                import_google_builds(c,path)
                self.assertEqual(c.execute('SELECT count(*) FROM source_build_catalog').fetchone()[0],2)
                pid=c.execute("SELECT id FROM source_products WHERE canonical_name='Pixel 9'").fetchone()[0]
                rows,total=product_source_builds(c,pid)
                self.assertEqual(total,1);self.assertEqual(rows[0]['android_version'],'15.0.0')
                for table in ('firmware_releases','product_firmware_releases','support_assertions','hardware_models','applicability_claims','domain_events'):
                    self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
            finally:db.close()

    def test_source_table_structure_and_hash_are_required(self):
        with self.assertRaises(ValueError):parse_builds('<table><tr><td>Unrelated table</td></tr></table>')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=self.capture(root);(root/'builds.html').write_text('changed')
            db=Database.migrated()
            try:
                with self.assertRaises(ValueError):import_google_builds(db.connection,path)
                self.assertEqual(db.connection.execute('SELECT count(*) FROM sources').fetchone()[0],0)
            finally:db.close()

    def test_pagination_and_unusual_comma_remain_exact(self):
        rows=parse_builds(HTML.replace('Pixel 9, Pixel 9 Pro','Pixel 9， Pixel 9 Pro'))
        self.assertEqual(rows[0]['supported_products'],['Pixel 9','Pixel 9 Pro'])
        self.assertEqual(rows[1]['supported_products'],[])
