from pathlib import Path
import json
import tempfile
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from mobile_observatory import Database
from mobile_observatory.server import ObservatoryService


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Database.migrated()
        self.service=ObservatoryService(self.db,Path(self.temp.name)/'local.sqlite',demonstration=False)
        self.db.connection.execute("INSERT INTO source_products VALUES('p1','Xiaomi','Example','example','proposed',NULL,'2026-01-01','2026-01-01')")
        self.proposal={'product_id':'p1','decision':'defer','canonical_name':'Example','model_codes':[],
           'aliases':[],'confidence':'low','evidence':[{'url':'https://example.com/spec','note':'Example research reference; no exact model code established'}],
           'rationale':'Insufficient evidence for an exact hardware identity'}

    def tearDown(self):
        self.service.local.close();self.db.close();self.temp.cleanup()

    def test_batch_validation_is_atomic_and_rejects_unsafe_or_missing_evidence(self):
        for invalid in ({**self.proposal,'product_id':'missing'}, {**self.proposal,'evidence':[]},
                        {**self.proposal,'evidence':[{'url':'javascript:alert(1)','note':'unsafe'}]},
                        {**self.proposal,'evidence':[{'artifact_id':'imaginary','note':'not captured'}]},
                        {**self.proposal,'confidence':'authoritative'}, {**self.proposal,'extra':True}):
            with self.assertRaises(ValueError):self.service.import_agent_proposals([self.proposal,invalid])
            self.assertEqual(self.service.agent_proposals(),[])

    def test_review_is_local_idempotent_remembered_and_preserves_correction_history(self):
        before=list(self.db.connection.iterdump())
        first=self.service.import_agent_proposals([self.proposal])
        self.assertEqual(first['canonicalChanges'],0)
        self.assertEqual(self.service.import_agent_proposals([self.proposal])['ids'],first['ids'])
        identifier=first['ids'][0]
        self.service.review_agent_proposal(identifier,{'decision':'different','rationale':'Conflicting hardware evidence'})
        self.service.import_agent_proposals([{**self.proposal,'rationale':'A repeated suggestion with new wording'}])
        self.assertTrue(all(x['status']=='rejected' for x in self.service.agent_proposals()))
        self.service.review_agent_proposal(identifier,{'decision':'same','rationale':'Human correction after inspecting evidence'})
        items=self.service.agent_proposals()
        self.assertTrue(all(x['status']=='approved' for x in items))
        reviewed=next(x for x in items if x['id']==identifier)
        self.assertEqual([r['decision'] for r in reviewed['reviews']],['different','same'])
        self.assertEqual(before,list(self.db.connection.iterdump()))
        self.service.local.close()
        self.service=ObservatoryService(self.db,Path(self.temp.name)/'local.sqlite',demonstration=False)
        self.assertTrue(all(x['status']=='approved' for x in self.service.agent_proposals()))

    def test_identity_memory_keeps_previous_decisions_and_one_current_null_target(self):
        value={'sourceNamespace':'example','sourceValue':'name','canonicalType':'hardware_model',
               'canonicalId':None,'decision':'different','rationale':'first review'}
        self.service.save_identity_decision(value)
        self.service.save_identity_decision({**value,'decision':'defer','rationale':'needs more evidence'})
        self.assertEqual(len(self.service.identity_decisions()),1)
        self.assertEqual(self.service.identity_decisions()[0]['decision'],'defer')
        self.assertEqual([x['decision'] for x in self.service.identity_history()],['defer','different'])

    def test_agent_bundle_suppresses_previously_reviewed_targets(self):
        bundle=Path(self.temp.name)/'agent-review';bundle.mkdir()
        (bundle/'agent-review-prompt.md').write_text('Assignment')
        (bundle/'identity-candidates.json').write_text(json.dumps([{'id':'p1','canonical_name':'Example'}]))
        identifier=self.service.import_agent_proposals([self.proposal])['ids'][0]
        self.service.review_agent_proposal(identifier,{'decision':'defer','rationale':'Wait for vendor evidence'})
        self.assertEqual(self.service.agent_review_bundle()['candidateCount'],0)
        self.assertEqual(self.service.agent_review_bundle()['rememberedReviews'][0]['status'],'deferred')
