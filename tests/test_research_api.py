from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from asgi_client import ASGITestClient
from backend.api.main import create_app
from backend.core.config import Settings
from backend.storage.catalog import import_catalog
from backend.storage.related_market import import_related_index
from backend.storage.research import canonical
from backend.integrations.research_inputs import capture_current_inputs
from scripts.research_pipeline import main as cli_main
from research_fixtures import *


class ResearchAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name)/'db.sqlite3')
        self.client=ASGITestClient(create_app(self.settings)); self.client.__enter__(); self.addCleanup(self.client.__exit__,None,None,None)
        self.time=clock(NOW); self.time.__enter__(); self.addCleanup(self.time.__exit__,None,None,None)

    def ingest(self):
        facts=full_facts()
        response=self.client.post('/api/research/facts',json_body={'facts':facts,'source_url':'https://example.test/fixture','raw_text':canonical(facts)})
        self.assertEqual(response.status_code,200)
        return response.json()

    def snapshot(self):
        response=self.client.post('/api/research/snapshots',json_body={'subjects':[SUBJECT,BENCHMARK,FUND_A,FUND_B]})
        self.assertEqual(response.status_code,200)
        return response.json()

    def test_status_and_health_report_capability_not_investment_success(self):
        self.assertEqual(self.client.get('/health').json()['schema_version'],12)
        status=self.client.get('/api/research/status').json()
        self.assertEqual(status['operation_status'],'unavailable')
        self.assertEqual(status['calendar_years'],[2025,2026])
        self.assertEqual(status['counts']['analysis_run'],0)

    def test_actual_asgi_ingest_freeze_run_replay_asset_workflow(self):
        result=self.ingest()
        snapshot=self.snapshot()
        run=self.client.post(f"/api/research/snapshots/{snapshot['snapshot_id']}/run")
        self.assertEqual(run.status_code,200)
        run_id=run.json()['run_id']
        self.assertTrue(self.client.post(f'/api/research/runs/{run_id}/replay').json()['matches'])
        self.assertEqual(self.client.get(f'/api/research/runs/{run_id}').json()['run_id'],run_id)
        self.assertEqual(self.client.get(f"/api/research/assets/{result['raw_asset_id']}").content,canonical(full_facts()).encode())
        self.assertEqual(len(self.client.get('/api/research/snapshots').json()['items']),1)

    def test_bad_input_is_422_not_hidden_500_or_silent_default(self):
        response=self.client.post('/api/research/facts',json_body={'facts':[numeric(first_seen_at=NOW)],'source_url':'https://example.test','raw_text':'x'})
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.client.post('/api/research/snapshots',json_body={'subjects':[SUBJECT],'mode':'public_as_of'}).status_code,422)
        self.assertEqual(self.client.post('/api/research/snapshots',json_body={'subjects':[SUBJECT],'created_at':NOW}).status_code,422)
        self.assertEqual(self.client.get('/api/research/snapshots/missing').status_code,404)
        self.assertEqual(self.client.post('/api/research/capture',json_body={}).status_code,422)

    def test_capture_uses_now_not_existing_import_date_and_returns_run(self):
        import_catalog(self.settings,[{'code':'510001','name':'测试','fund_type':'ETF'}],policy_version='fixture')
        row={'date':'2026-09-16','open':'10','high':'12','low':'9','close':'11','volume':None,'amount':None}
        import_related_index(self.settings,fund_code='510001',index_code='000016',index_name='测试指数',rows=[row],source_id='fixture.index',relation_source_id='fixture.prospectus',evidence_url='https://example.test/prospectus')
        response=self.client.post('/api/research/capture',json_body={})
        self.assertEqual(response.status_code,200)
        self.assertIn('run_id',response.json())
        snapshot=self.client.get('/api/research/snapshots/'+response.json()['snapshot_id']).json()
        self.assertEqual(snapshot['manifest']['facts'][0]['first_seen_at'],NOW)
        self.assertEqual(response.json()['fidelity'],'local_projection_not_original_response')
        self.assertFalse(any(f.get('series_kind')=='total_return' for f in snapshot['manifest']['facts']))

    def test_actual_asgi_trial_forward_score_and_history_contract(self):
        with clock('2026-09-16T12:58:00.000000Z'):
            trial=self.client.post('/api/research/trials',json_body=specification())
        self.assertEqual(trial.status_code,200)
        trial_id=trial.json()['trial_id']
        self.ingest(); snapshot=self.snapshot()
        run=self.client.post(f"/api/research/snapshots/{snapshot['snapshot_id']}/run").json()
        forward=self.client.post(f'/api/research/trials/{trial_id}/forward',json_body={'run_id':run['run_id']})
        self.assertEqual(forward.status_code,200)
        self.assertEqual(self.client.post(f'/api/research/trials/{trial_id}/forward',json_body={'run_id':run['run_id'],'created_at':NOW}).status_code,422)
        for action in ('score','history'):
            report=self.client.post(f'/api/research/trials/{trial_id}/{action}',json_body={'outcome_snapshot_id':snapshot['snapshot_id']})
            self.assertEqual(report.status_code,200)
            self.assertFalse(report.json()['investment_operations_enabled'])

    def test_quarantine_only_changes_new_snapshots(self):
        result=self.ingest(); before=self.snapshot()
        revision=result['revision_ids'][0]
        response=self.client.post(f'/api/research/facts/{revision}/quarantine',json_body={'reason':'测试反证'})
        self.assertEqual(response.status_code,200)
        after=self.snapshot()
        self.assertNotEqual(after['snapshot_id'],before['snapshot_id'])
        self.assertTrue(any(f['revision_id']==revision for f in self.client.get('/api/research/snapshots/'+before['snapshot_id']).json()['manifest']['facts']))

    def test_cli_ingest_real_file_and_status_and_failure_exit(self):
        raw=Path(self.temp.name)/'original.txt'; raw.write_text('明确标记为测试原文')
        spec=Path(self.temp.name)/'input.json';spec.write_text(json.dumps({'facts':[numeric()],'source_url':'https://example.test/fixture','raw_file':'original.txt'},ensure_ascii=False))
        from io import StringIO
        output=StringIO()
        with patch('sys.stdout',output):
            code=cli_main(['--database',str(self.settings.database_path),'ingest','--input',str(spec)])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.getvalue())['new_revision_count'],1)
        with patch('sys.stderr',StringIO()):
            self.assertEqual(cli_main(['--database',str(self.settings.database_path),'run','--snapshot-id','missing']),2)
