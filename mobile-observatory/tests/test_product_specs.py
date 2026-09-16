from __future__ import annotations
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from mobile_observatory.database import Database
from mobile_observatory.product_specs import enrich_product_specs

NOW = '2026-01-01T00:00:00Z'

class ProductSpecsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database.migrated(self.root / 'corpus.sqlite')
        self.c = self.db.connection
        self.c.execute("INSERT INTO sources VALUES('test','Test',NULL,'primary',1,?)", (NOW,))
        self.catalog = self.root / 'devices.yml'
        self.catalog.write_text('')
        self.specs = self.root / 'specs.csv'

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def product(self, name, code='test', state='approved', maker='Xiaomi'):
        self.c.execute('INSERT INTO source_products VALUES(?,?,?,?,?,NULL,?,?)',
                       (name,maker,name,name.lower(),state,NOW,NOW))
        self.c.execute('INSERT INTO source_identity_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (name,'test','codename',code,code,name,state,'test','1','high',NOW,NOW))

    def run_specs(self, names, decisions=None, play=None):
        with self.specs.open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=['brand','device_name','slug','chipset','fetched_at'])
            writer.writeheader()
            for name,slug,chip in names:
                writer.writerow(dict(brand='Xiaomi',device_name=name,slug=slug,chipset=chip,fetched_at=NOW))
        return enrich_product_specs(self.c,specs_csv=self.specs,devices_yml=self.catalog,
                                    google_play_csv=play,decisions=decisions)

    def test_standalone_specs_are_idempotent_and_preserve_capture_not_hardware(self):
        rows=[('Xiaomi New Phone','phone-1.php','Qualcomm SM1 (4 nm)')]
        first=self.run_specs(rows)
        self.assertEqual(first['specification_only_products'],1)
        self.run_specs(rows)
        self.assertEqual(self.c.execute('SELECT count(*) FROM source_products').fetchone()[0],1)
        self.assertEqual(self.c.execute('SELECT count(*) FROM hardware_models').fetchone()[0],0)
        self.assertEqual(self.c.execute('SELECT count(*) FROM product_firmware_releases').fetchone()[0],0)
        evidence=json.loads(self.c.execute('SELECT evidence_json FROM observed_product_silicon').fetchone()[0])[0]
        self.assertEqual(evidence['source_url'],'https://www.gsmarena.com/phone-1.php')
        self.assertEqual(evidence['locator'],'csv:line=2')
        artifact=self.c.execute('SELECT * FROM artifacts').fetchone()
        self.assertEqual(artifact['sha256'],evidence['artifact_sha256'])
        self.assertEqual(Path(artifact['storage_uri']).read_bytes(),self.specs.read_bytes())

    def test_plus_network_and_brand_are_not_erased(self):
        self.product('Phone Pro+',maker='Xiaomi')
        self.product('Phone Pro',code='other',maker='TECNO')
        self.run_specs([('Xiaomi Phone Pro','pro.php','Chip'),('Xiaomi Phone Pro 5G','5g.php','Other')])
        ids={r[0] for r in self.c.execute('SELECT product_id FROM observed_product_silicon')}
        self.assertNotIn('Phone Pro+',ids)
        self.assertNotIn('Phone Pro',ids)

    def test_catalog_alias_and_play_corroboration_keep_spec_proof(self):
        self.product('Local Phone','ruby')
        self.catalog.write_text('ruby:\n- Xiaomi Phone Global\n- RUBY\n')
        play=self.root/'play.csv'
        play.write_text('Retail Branding,Marketing Name,Device,Model\nXiaomi,Phone,ruby,EXACT-1\n')
        self.run_specs([('Xiaomi Phone','phone.php','Qualcomm SM1')],play=play)
        proof=json.loads(self.c.execute("SELECT evidence_json FROM observed_product_silicon WHERE product_id='Local Phone'").fetchone()[0])
        self.assertEqual(proof[0]['source'],'gsmarena_captured_specs')
        self.assertEqual(proof[1]['source'],'xiaomi_devices_yml')
        self.assertEqual(self.c.execute('SELECT count(*) FROM source_products').fetchone()[0],1)

    def test_conflicting_aliases_and_duplicate_assertions_do_not_merge(self):
        self.product('Local Phone','ruby')
        self.catalog.write_text('ruby:\n- Phone Global\n- Other Global\n')
        result=self.run_specs([('Xiaomi Phone','phone.php','Chip'),('Xiaomi Other','other.php','Other chip')])
        self.assertEqual(result['ambiguous_products'],1)
        self.assertIsNone(self.c.execute("SELECT 1 FROM observed_product_silicon WHERE product_id='Local Phone'").fetchone())
        self.product('Duplicate','duplicate')
        self.run_specs([('Xiaomi Duplicate','same.php','One'),('Xiaomi Duplicate','same.php','Two')])
        self.assertIsNone(self.c.execute("SELECT 1 FROM observed_product_silicon WHERE product_id='Duplicate'").fetchone())

    def test_remembered_rejection_and_defer_block_enrichment(self):
        self.product('Rejected','reject','rejected')
        self.product('Deferred','defer')
        self.run_specs([('Xiaomi Rejected','reject.php','Chip'),('Xiaomi Deferred','defer.php','Chip')],
                       decisions=[{'canonical_id':'Deferred','decision':'defer'}])
        self.assertEqual(self.c.execute('SELECT count(*) FROM observed_product_silicon').fetchone()[0],0)
        self.assertEqual(self.c.execute("SELECT review_state FROM source_products WHERE id='Rejected'").fetchone()[0],'rejected')

    def test_replay_preserves_remembered_conclusions_and_manual_rejection(self):
        from mobile_observatory.enrichment import automate_identity_review
        self.product('Rejected','reject','rejected')
        self.product('Remembered','remembered')
        self.run_specs([('Xiaomi Rejected','reject.php','Chip')])
        self.c.execute('INSERT INTO identity_conclusions VALUES(?,?,?,?,?,?,?,?,?)',
            ('Remembered','ambiguous','medium','remembered_review','1','Keep distinct','[]','[]',NOW))
        before=tuple(self.c.execute("SELECT * FROM identity_conclusions WHERE product_id='Remembered'").fetchone())
        automate_identity_review(self.c,specs_csv=self.specs,devices_yml=self.catalog)
        self.assertEqual(tuple(self.c.execute("SELECT * FROM identity_conclusions WHERE product_id='Remembered'").fetchone()),before)
        self.assertEqual(self.c.execute("SELECT review_state FROM source_products WHERE id='Rejected'").fetchone()[0],'rejected')
        self.assertIsNone(self.c.execute("SELECT 1 FROM observed_product_silicon WHERE product_id='Rejected'").fetchone())
