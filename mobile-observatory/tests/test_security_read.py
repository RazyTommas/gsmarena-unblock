from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from mobile_observatory import Database
from mobile_observatory.seed import seed_demonstration
from mobile_observatory.server import ObservatoryService


class SecurityReadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Database.migrated()
        seed_demonstration(self.db,ROOT/'fixtures/supported_catalog.sample.json')
        self.service=ObservatoryService(self.db,Path(self.temp.name)/'local.sqlite',demonstration=True)
        c=self.db.connection
        self.advisory='test-advisory'
        source=c.execute('SELECT id FROM sources LIMIT 1').fetchone()[0]
        evidence=c.execute('SELECT id FROM evidence LIMIT 1').fetchone()[0]
        c.execute('INSERT INTO advisories VALUES(?,?,?,?,?,?,?)',(self.advisory,source,'test','Test bulletin','2099-01-01T00:00:00Z',None,evidence))
        self.evidence=c.execute('SELECT id FROM evidence LIMIT 1').fetchone()[0]
        self.part=c.execute('SELECT part_id FROM hardware_silicon LIMIT 1').fetchone()[0]
        self.part_number=c.execute('SELECT part_number FROM silicon_parts WHERE id=?',(self.part,)).fetchone()[0]
        for i in range(125):
            c.execute('INSERT INTO vulnerabilities VALUES(?,?,?,?,?)',(f'test-v-{i}',f'CVE-2099-{10000+i}',f'test component {i}','2099-01-01T00:00:00Z',None))
            c.execute('INSERT INTO advisory_vulnerabilities VALUES(?,?)',(self.advisory,f'test-v-{i}'))
        c.execute('INSERT INTO applicability_claims VALUES(?,?,?,?,?,?,?,?)',('test-ac','test-v-0','silicon_part',self.part,'affected','{"versions":["source-specific"]}',self.evidence,'2099-01-01T00:00:00Z'))
        c.execute('INSERT INTO fix_claims VALUES(?,?,?,?,?,?)',('test-fix','test-v-0','component_version','{"component":"different component","version":"1"}',self.evidence,'2099-01-02T00:00:00Z'))
        c.execute('INSERT INTO applicability_claims VALUES(?,?,?,?,?,?,?,?)',('test-negative','test-v-1','silicon_part',self.part,'not_affected','{}',self.evidence,'2099-01-01T00:00:00Z'))

    def tearDown(self):
        self.service.local.close();self.db.close();self.temp.cleanup()

    def test_detail_is_independent_of_first_page_and_preserves_unrelated_fix_scope(self):
        first=self.service.security_page({'limit':['100']})
        self.assertNotIn('CVE-2099-10000',[x['cve'] for x in first.items])
        detail=self.service.security_detail('CVE-2099-10000')
        self.assertEqual(detail['fixes'][0]['coordinate']['component'],'different component')
        self.assertEqual(detail['claims'][0]['constraint'],{'versions':['source-specific']})
        self.assertTrue(detail['mappedHardware'])
        self.assertEqual(detail['verdicts'],[])
        self.assertIn('not established',detail['reasoning'])
        self.assertIn('not proof',detail['boundaries']['fixCoordinates'])
        self.assertEqual(len(detail['claims'][0]['sha256']),64)
        with self.assertRaises(KeyError):self.service.security_detail('CVE-2099-99999')

    def test_filters_are_global_exact_and_do_not_treat_negative_claim_as_affected(self):
        q={'q':['CVE-2099'],'exact_part':['1'],'part':[self.part_number],'mobile_linked':['1']}
        page=self.service.security_page(q)
        self.assertEqual([x['cve'] for x in page.items],['CVE-2099-10000'])
        self.assertEqual(self.service.security_page({**q,'fix_status':['without_coordinate']}).total,0)
        self.assertEqual(self.service.security_page({**q,'fix_status':['with_coordinate']}).total,1)
        self.assertEqual(self.service.security_page({**q,'part':[self.part_number+'unknown']}).total,0)
        self.assertEqual(self.service.security_page({**q,'date_to':['2098-12-31']}).total,0)
        self.assertEqual(self.service.security_page({**q,'date_from':['2099-01-01'],'date_to':['2099-01-01']}).total,1)
        self.assertEqual(self.service.security_page({**q,'vendor':['not-a-source']}).total,0)
        self.assertEqual(self.service.security_page({'q':['CVE-2099'],'limit':['100'],'offset':['100']}).total,125)
        self.assertEqual(len(self.service.security_page({'q':['CVE-2099'],'offset':['100']}).items),25)

    def test_index_migration_is_idempotent(self):
        self.db.apply_migrations()
        indexes={r[0] for r in self.db.connection.execute("SELECT name FROM sqlite_schema WHERE type='index'")}
        self.assertIn('fix_vulnerability_idx',indexes)
        self.assertIn('applicability_vulnerability_idx',indexes)
