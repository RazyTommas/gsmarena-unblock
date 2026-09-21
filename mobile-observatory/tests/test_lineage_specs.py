from __future__ import annotations
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from mobile_observatory.database import Database
from mobile_observatory.lineage_specs import enrich_lineage_specs, hardware_specification_evidence

NOW='2026-09-17T00:00:00Z'
COMMIT='a'*40


class LineageSpecificationsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.db=Database.migrated(self.root/'corpus.sqlite')
        self.c=self.db.connection

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def capture(self, records=None):
        records=records or [{'key':'test','vendor':'Samsung','name':'Galaxy Test','codename':'test',
                           'soc':'Samsung Exynos 990','models':['SM-TEST','SM-TEST/DS']}]
        for row in records:
            data=json.dumps(row).encode()
            (self.root/(row['key']+'.yml')).write_bytes(data)
            row.update(raw_path=row['key']+'.yml',sha256=hashlib.sha256(data).hexdigest(),observed_at=NOW,
                source_url=f"https://raw.githubusercontent.com/LineageOS/lineage_wiki/{COMMIT}/_data/devices/{row['key']}.yml")
        path=self.root/'capture.json'
        path.write_text(json.dumps(dict(format_version=1,commit=COMMIT,records=records)))
        return path

    def test_exact_capture_is_idempotent_and_does_not_create_hardware_or_android(self):
        p=self.capture()
        enrich_lineage_specs(self.c,p)
        enrich_lineage_specs(self.c,p)
        for table,n in [('source_products',1),('source_specifications',1),('observed_product_silicon',1),
                        ('hardware_models',0),('hardware_silicon',0),('firmware_releases',0),('os_releases',0)]:
            self.assertEqual(self.c.execute('SELECT count(*) FROM '+table).fetchone()[0],n)
        proof=json.loads(self.c.execute('SELECT evidence_json FROM observed_product_silicon').fetchone()[0])[0]
        self.assertEqual(proof['source_commit'],COMMIT)
        self.assertNotIn('slug',proof)
        self.assertEqual(self.c.execute('SELECT authority_scope FROM sources').fetchone()[0],'secondary')

    def test_capture_tampering_rolls_back_before_writes(self):
        p=self.capture()
        (self.root/'test.yml').write_text('changed')
        with self.assertRaises(ValueError):enrich_lineage_specs(self.c,p)
        self.assertEqual(self.c.execute('SELECT count(*) FROM sources').fetchone()[0],0)

    def test_conflicting_chip_retains_old_fact_and_new_unlinked_assertion(self):
        p=self.capture()
        enrich_lineage_specs(self.c,p)
        self.c.execute("UPDATE observed_product_silicon SET raw_chipset='Other chip'")
        enrich_lineage_specs(self.c,p)
        self.assertEqual(self.c.execute('SELECT raw_chipset FROM observed_product_silicon').fetchone()[0],'Other chip')
        self.assertEqual(self.c.execute('SELECT count(*) FROM source_products').fetchone()[0],1)

    def test_variants_sharing_codename_keep_separate_source_products(self):
        rows=[dict(key='phone_variant'+str(i),vendor='Samsung',name='Phone',codename='phone',
                   soc='Chip '+str(i),models=['SM-'+str(i)]) for i in (1,2)]
        enrich_lineage_specs(self.c,self.capture(rows))
        self.assertEqual(self.c.execute('SELECT count(*) FROM source_products').fetchone()[0],2)
        self.assertEqual(self.c.execute('SELECT count(DISTINCT product_id) FROM source_specifications').fetchone()[0],2)

    def test_review_blocks_silicon_without_losing_captured_source(self):
        p=self.capture()
        self.c.execute('INSERT INTO source_products VALUES(?,?,?,?,?,?,?,?)',
            ('prior','Samsung','Galaxy Test','galaxy test','rejected',None,NOW,NOW))
        result=enrich_lineage_specs(self.c,p)
        self.assertEqual(result['blocked_by_review'],1)
        self.assertEqual(self.c.execute('SELECT count(*) FROM observed_product_silicon').fetchone()[0],0)
        self.assertIsNone(self.c.execute('SELECT product_id FROM source_specifications').fetchone()[0])

    def test_hardware_evidence_requires_literal_model_and_brand(self):
        # Independent exact-model lookup on the canonical catalog view is tested
        # with a minimal canonical chain; no inferred /DS or region variants.
        self.c.execute('INSERT INTO manufacturers VALUES(?,?,?,?,?)',('m','Samsung',None,NOW,NOW))
        self.c.execute('INSERT INTO brands VALUES(?,?,?,?,?)',('b','m','Samsung',NOW,NOW))
        self.c.execute('INSERT INTO device_families VALUES(?,?,?,?,?,?)',('f','b','Test',None,NOW,NOW))
        self.c.execute('INSERT INTO device_variants VALUES(?,?,?,?,?,?,?)',('v','f','Test','phone',None,NOW,NOW))
        self.c.execute('INSERT INTO hardware_models VALUES(?,?,?,?,?,?,?)',('h','v','SM-TEST','SM-TEST',None,NOW,NOW))
        enrich_lineage_specs(self.c,self.capture())
        self.assertEqual(len(hardware_specification_evidence(self.c,'SM-TEST')),1)
        self.assertFalse(hardware_specification_evidence(self.c,'SM-TEST')[0]['canonical_mapping'])
        self.assertEqual(hardware_specification_evidence(self.c,'SM-TESTU'),[])
