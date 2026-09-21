from __future__ import annotations
import csv
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).parents[1]/'src'))
from mobile_observatory.database import Database
from mobile_observatory.hmd_updates import import_hmd_updates,parse_update
from mobile_observatory.enrichment import promote_approved_product_observations

ROW=dict(phone='HMD Test',androidBulletinDate='2026-05-01',cadence='Quarterly',endOfLife='May-27',
    spVersion='May-26',screenId='00WW_BUILD',comments='SP Only (Android 15)',cadenceComments='Estimated',
    dateOfFirstLiveRelease='July 06, 2026')
NOW='2026-09-13T11:04:49Z'


class HmdUpdatesTests(unittest.TestCase):
    def test_vendor_field_meanings_remain_separate(self):
        data=parse_update(ROW)
        self.assertEqual(data['security_patch_level'],'2026-05-01')
        self.assertEqual(data['release_date'],'2026-07-06')
        self.assertEqual(data['android'],'15')
        self.assertEqual(data['region_code'],'SOURCE_UNSPECIFIED')
        self.assertNotIn('model_code',data)
        self.assertNotIn('download_url',data)
        self.assertNotIn('publish_date',data)
        self.assertIsNone(parse_update(dict(ROW,comments='SP Only (Nougat)'))['android'])
        self.assertIsNone(parse_update(dict(ROW,comments='Android 14 / Android 15 variants'))['android'])

    def test_import_is_idempotent_without_canonical_or_upgrade_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/'updates.csv'
            with path.open('w') as f:
                writer=csv.DictWriter(f,fieldnames=ROW);writer.writeheader();writer.writerow(ROW)
                writer.writerow(dict(ROW,comments='SP Only (Android 16)',screenId='00WW_BUILD_2',dateOfFirstLiveRelease='August 01, 2026'))
            db=Database.migrated(root/'corpus.sqlite');c=db.connection
            try:
                self.assertEqual(import_hmd_updates(c,path,observed_at=NOW)['firmware_added'],2)
                self.assertEqual(import_hmd_updates(c,path,observed_at=NOW)['firmware_added'],0)
                promote_approved_product_observations(c)
                for table,expected in [('source_products',1),('product_firmware_releases',2),
                    ('product_security_publications',2),('hardware_models',0),('hardware_silicon',0),
                    ('domain_events',0),('support_assertions',0),('applicability_claims',0)]:
                    self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],expected)
                self.assertIsNone(c.execute('SELECT published_at FROM product_security_publications').fetchone()[0])
                self.assertEqual(c.execute('SELECT count(*) FROM observations').fetchone()[0],2)
                self.assertEqual(c.execute('SELECT authority_scope FROM sources').fetchone()[0],'primary')
            finally:db.close()

    def test_rejected_product_keeps_raw_observations_without_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'updates.csv'
            with path.open('w') as f:
                writer=csv.DictWriter(f,fieldnames=ROW);writer.writeheader();writer.writerow(ROW)
            db=Database.migrated();c=db.connection
            try:
                c.execute('INSERT INTO source_products VALUES(?,?,?,?,?,?,?,?)',('p','HMD','HMD Test','test','rejected',None,NOW,NOW))
                self.assertEqual(import_hmd_updates(c,path,observed_at=NOW)['blocked_rows'],1)
                self.assertEqual(c.execute('SELECT count(*) FROM observations').fetchone()[0],1)
                self.assertEqual(c.execute('SELECT count(*) FROM product_firmware_releases').fetchone()[0],0)
            finally:db.close()
