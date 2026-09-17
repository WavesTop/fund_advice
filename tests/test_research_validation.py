import copy
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from backend.analysis.research_pipeline import run_snapshot
from backend.analysis.validation import register_trial, register_forward, score_forward, validate_history, get_trial, _outcome_calendar
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.database import connection_scope
from backend.storage.research import append_facts, freeze_snapshot, canonical
from research_fixtures import *


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name)/'db.sqlite3')

    def prepare(self, spec=None):
        with clock('2026-09-16T12:58:00.000000Z'):
            trial = register_trial(self.settings,spec or specification())
        with clock(NOW):
            facts = full_facts()
            append_facts(self.settings,facts,source_url='https://example.test/fixture',raw_body=canonical(facts).encode())
            snapshot = freeze_snapshot(self.settings,[SUBJECT,BENCHMARK,FUND_A,FUND_B])
            run = run_snapshot(self.settings,snapshot['snapshot_id'])
        return trial,snapshot,run

    def outcomes(self, *, missing=False):
        facts = series(SUBJECT,kind='total_return',end='2026-10-30',count=80) + series(BENCHMARK,kind='total_return',end='2026-10-30',count=80,rate='0.0005')
        if missing: facts = [f for f in facts if f['effective_date']!='2026-09-18']
        with clock('2026-10-30T13:00:00.000000Z'):
            append_facts(self.settings,facts,source_url='https://example.test/fixture-outcome',raw_body=canonical(facts).encode())
            return freeze_snapshot(self.settings,[SUBJECT,BENCHMARK])

    def test_protocol_idempotence_and_new_parameters_preserve_all_trials(self):
        with clock(NOW):
            first=register_trial(self.settings,specification())
            repeat=register_trial(self.settings,specification())
            second=register_trial(self.settings,specification(signal_rule='evidence_watch'))
        self.assertEqual(first['trial_id'],repeat['trial_id'])
        self.assertNotEqual(first['trial_id'],second['trial_id'])
        self.assertFalse(first['protocol']['enable_investment_operations'])
        with connection_scope(self.settings) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM validation_trial').fetchone()[0],2)

    def test_invalid_ranges_and_insufficient_embargo_are_rejected(self):
        invalid = [specification(validation={'start':'2025-06-19','end':'2025-12-20'}),
                   specification(validation={'start':'2025-06-23','end':'2025-12-20'},embargo_sessions=5),
                   specification(test={'start':'2027-01-01','end':'2027-12-31'}),
                   specification(horizon_sessions=20.0)]
        with clock(NOW):
            for spec in invalid:
                with self.subTest(spec=spec),self.assertRaises(ValueError): register_trial(self.settings,spec)

    def test_cannot_backdate_protocol_or_pass_hidden_optimizations(self):
        with clock(NOW):
            for extra in ({'created_at':'2025-01-01T00:00:00Z'},{'implementation_hash':'fake'},{'best_weight':.4}):
                with self.assertRaises(ValueError): register_trial(self.settings,{**specification(),**extra})

    def test_forward_requires_protocol_first_and_fresh_actual_run(self):
        trial,snapshot,run=self.prepare()
        with clock('2026-09-16T13:06:00.000000Z'),self.assertRaises(ValueError):
            register_forward(self.settings,trial['trial_id'],run['run_id'])
        with clock('2026-09-16T13:01:00.000000Z'):
            late_trial=register_trial(self.settings,specification(name='事后协议'))
            with self.assertRaises(ValueError): register_forward(self.settings,late_trial['trial_id'],run['run_id'])

    def test_forward_registration_is_idempotent_and_not_matured_is_not_pass(self):
        trial,snapshot,run=self.prepare()
        with clock(NOW):
            first=register_forward(self.settings,trial['trial_id'],run['run_id'])
            again=register_forward(self.settings,trial['trial_id'],run['run_id'])
            report=score_forward(self.settings,trial['trial_id'],snapshot['snapshot_id'])
        self.assertEqual(first['observations'],again['observations'])
        self.assertEqual(first['entry_date'],'2026-09-17')
        self.assertEqual(report['summary']['status_counts'],{'not_matured':1})
        self.assertFalse(report['investment_operations_enabled'])
        self.assertEqual(report['investment_effectiveness'],'not_established')
        with connection_scope(self.settings) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM forward_observation').fetchone()[0],1)

    def test_forward_total_return_label_is_independently_recomputed(self):
        trial,snapshot,run=self.prepare()
        with clock(NOW): register_forward(self.settings,trial['trial_id'],run['run_id'])
        later=self.outcomes()
        with clock('2026-10-30T13:01:00.000000Z'):
            report=score_forward(self.settings,trial['trial_id'],later['snapshot_id'])
        case=report['cases'][0]
        self.assertEqual(case['status'],'matured')
        expected=((Decimal('1.001')**20)-(Decimal('1.0005')**20))*100
        self.assertLess(abs(Decimal(case['excess_label_return_pp'])-expected),Decimal('1e-20'))
        self.assertEqual(report['summary']['sample_status'],'insufficient')
        self.assertIsNone(report['summary']['confidence_interval'])
        self.assertIn('不是可成交策略净值',report['return_interpretation'])

    def test_missing_total_return_is_never_zero_performance(self):
        trial,snapshot,run=self.prepare()
        with clock(NOW): register_forward(self.settings,trial['trial_id'],run['run_id'])
        with clock('2026-10-30T13:00:00.000000Z'):
            later=freeze_snapshot(self.settings,[SUBJECT,BENCHMARK])
            report=score_forward(self.settings,trial['trial_id'],later['snapshot_id'])
        self.assertEqual(report['cases'][0]['status'],'data_insufficient')
        self.assertNotIn('asset_label_return_pct',report['cases'][0])

    def test_overlapping_observations_are_purged_before_aggregate(self):
        trial,snapshot,run=self.prepare()
        with clock(NOW): register_forward(self.settings,trial['trial_id'],run['run_id'])
        with clock('2026-09-16T13:01:00.000000Z'):
            other_snapshot=freeze_snapshot(self.settings,[SUBJECT,BENCHMARK,FUND_A,FUND_B])
            other_run=run_snapshot(self.settings,other_snapshot['snapshot_id'])
            register_forward(self.settings,trial['trial_id'],other_run['run_id'])
        later=self.outcomes()
        with clock('2026-10-30T13:01:00.000000Z'):
            report=score_forward(self.settings,trial['trial_id'],later['snapshot_id'])
        self.assertEqual([case['status'] for case in report['cases']],['matured','purged_overlapping_window'])
        self.assertEqual(report['summary']['matured_count'],1)

    def test_failed_first_overlap_not_replaced_with_better_later_observation(self):
        trial,snapshot,run=self.prepare()
        with clock(NOW): register_forward(self.settings,trial['trial_id'],run['run_id'])
        with clock('2026-09-16T13:01:00.000000Z'):
            other_snapshot=freeze_snapshot(self.settings,[SUBJECT,BENCHMARK,FUND_A,FUND_B])
            other_run=run_snapshot(self.settings,other_snapshot['snapshot_id'])
            register_forward(self.settings,trial['trial_id'],other_run['run_id'])
        with clock('2026-10-30T13:00:00.000000Z'):
            later=freeze_snapshot(self.settings,[SUBJECT,BENCHMARK])
            report=score_forward(self.settings,trial['trial_id'],later['snapshot_id'])
        self.assertEqual([case['status'] for case in report['cases']],['data_insufficient','purged_overlapping_window'])

    def test_label_crossing_split_boundary_is_purged(self):
        spec=specification(test={'start':'2026-01-05','end':'2026-09-30'})
        trial,snapshot,run=self.prepare(spec)
        with clock(NOW):
            register_forward(self.settings,trial['trial_id'],run['run_id'])
            report=score_forward(self.settings,trial['trial_id'],snapshot['snapshot_id'])
        self.assertEqual(report['cases'][0]['status'],'purged_split_boundary')

    def test_uncovered_future_long_horizon_has_explicit_calendar_gap(self):
        trial,snapshot,run=self.prepare(specification(horizon_sessions=120,period='long'))
        with clock(NOW):
            register_forward(self.settings,trial['trial_id'],run['run_id'])
            report=score_forward(self.settings,trial['trial_id'],snapshot['snapshot_id'])
        self.assertEqual(report['cases'][0]['status'],'calendar_unavailable')
        self.assertEqual(report['summary']['matured_count'],0)

    def test_complete_universe_cannot_be_cherry_picked_at_registration(self):
        spec=specification(subjects=[SUBJECT,'tracked_index:fixture:unseen'])
        trial,snapshot,run=self.prepare(spec)
        with clock(NOW),self.assertRaises(ValueError): register_forward(self.settings,trial['trial_id'],run['run_id'])

    def test_historical_backfill_stays_missing_and_is_not_unseen_oos(self):
        trial,snapshot,run=self.prepare()
        with clock(NOW): report=validate_history(self.settings,trial['trial_id'],snapshot['snapshot_id'])
        self.assertEqual(report['mode'],'retrospective_chronological_diagnostic_not_unseen_oos')
        self.assertGreater(report['summary']['status_counts']['missing_system_known_input'],0)
        self.assertGreater(report['summary']['status_counts']['purged_split_boundary'],0)
        self.assertEqual(set(report['phases']),{'development','validation','test'})
        self.assertFalse(report['investment_operations_enabled'])

    def test_calendar_extension_preserves_old_years_and_changes_are_rejected(self):
        trial,snapshot,run=self.prepare()
        current=copy.deepcopy(snapshot['manifest'])
        current['calendar']['years']['2027']={'sources':['https://example.test/calendar-fixture-not-real'],'closed_ranges':[]}
        expanded=_outcome_calendar(trial['protocol'],current)
        self.assertIn(2027,expanded.years)
        current['calendar']['years']['2026']['closed_ranges'].append(['2026-09-16','2026-09-16'])
        with self.assertRaises(ValueError): _outcome_calendar(trial['protocol'],current)

    def test_each_maturity_report_is_append_only(self):
        trial,snapshot,run=self.prepare()
        with clock(NOW):
            register_forward(self.settings,trial['trial_id'],run['run_id'])
            immature=score_forward(self.settings,trial['trial_id'],snapshot['snapshot_id'])
        later=self.outcomes()
        with clock('2026-10-30T13:01:00.000000Z'):
            mature=score_forward(self.settings,trial['trial_id'],later['snapshot_id'])
        self.assertNotEqual(immature['report_id'],mature['report_id'])
        with connection_scope(self.settings) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM validation_report').fetchone()[0],2)
            import sqlite3
            with self.assertRaises(sqlite3.IntegrityError): connection.execute('DELETE FROM validation_report')
