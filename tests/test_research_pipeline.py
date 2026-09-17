import copy
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.analysis.fund_selection import compare_passive_funds, _tracking
from backend.analysis.research_pipeline import run_snapshot, get_run, replay_run, assess_subject
from backend.analysis.total_return import reinvested_nav
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.core.trading_calendar import get_calendar
from backend.storage.database import connection_scope
from backend.storage.research import append_facts, freeze_snapshot, quarantine, canonical
from research_fixtures import *


class TotalReturnTests(unittest.TestCase):
    def calculate(self, nav, actions, **kwargs):
        return reinvested_nav(nav, actions, calendar=get_calendar(), coverage_start='2026-09-14', coverage_end='2026-09-16', actions_complete=True, **kwargs)

    def test_cash_dividend_does_not_create_price_loss(self):
        result = self.calculate([{'date':'2026-09-14','unit_nav':'1'}, {'date':'2026-09-15','unit_nav':'.9'}],
                                [{'date':'2026-09-15','cash_per_old_share':'.1','new_shares_per_old_share':'1'}])
        self.assertEqual(Decimal(result[-1]['value']), Decimal(1))

    def test_split_is_per_old_share_and_does_not_create_loss(self):
        result = self.calculate([{'date':'2026-09-14','unit_nav':'1'}, {'date':'2026-09-15','unit_nav':'.5'}],
                                [{'date':'2026-09-15','cash_per_old_share':'0','new_shares_per_old_share':'2'}])
        self.assertEqual(Decimal(result[-1]['value']), Decimal(1))

    def test_combined_cash_split_and_subsequent_gain_independent_example(self):
        result = self.calculate([{'date':'2026-09-14','unit_nav':'10'}, {'date':'2026-09-15','unit_nav':'4.5'}, {'date':'2026-09-16','unit_nav':'4.95'}],
                                [{'date':'2026-09-15','cash_per_old_share':'1','new_shares_per_old_share':'2'}])
        self.assertEqual(Decimal(result[-1]['value']), Decimal('1.1'))

    def test_missing_action_coverage_is_not_zero_cash(self):
        with self.assertRaises(ValueError):
            reinvested_nav([{'date':'2026-09-14','unit_nav':'1'},{'date':'2026-09-15','unit_nav':'1'}], [], calendar=get_calendar(), coverage_start='2026-09-14', coverage_end='2026-09-16', actions_complete=False)

    def test_duplicate_events_and_missing_nav_day_fail(self):
        event = {'date':'2026-09-15','cash_per_old_share':'0','new_shares_per_old_share':'1'}
        with self.assertRaises(ValueError):
            self.calculate([{'date':'2026-09-14','unit_nav':'1'},{'date':'2026-09-15','unit_nav':'1'}], [event,event])
        with self.assertRaises(ValueError):
            self.calculate([{'date':'2026-09-14','unit_nav':'1'},{'date':'2026-09-16','unit_nav':'1'}], [])


class SnapshotResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name)/'db.sqlite3')
        self.time = clock(NOW); self.time.__enter__(); self.addCleanup(self.time.__exit__,None,None,None)

    def snapshot(self, facts=None):
        facts = full_facts() if facts is None else facts
        append_facts(self.settings, facts, source_url='https://example.test/test-only', raw_body=canonical(facts).encode())
        return freeze_snapshot(self.settings,[SUBJECT,BENCHMARK,FUND_A,FUND_B])

    def compare(self, facts=None):
        return compare_passive_funds(self.snapshot(facts)['manifest'],SUBJECT)

    def test_deterministic_run_and_replay_use_only_original_inputs(self):
        snapshot = self.snapshot()
        run = run_snapshot(self.settings,snapshot['snapshot_id'])
        self.assertEqual(run_snapshot(self.settings,snapshot['snapshot_id'])['run_id'], run['run_id'])
        append_facts(self.settings,[numeric(value='-99')],source_url='https://example.test/correction',raw_body=b'correction')
        self.assertTrue(replay_run(self.settings,run['run_id'])['matches'])
        self.assertEqual(get_run(self.settings,run['run_id'])['output_hash'],run['output_hash'])
        self.assertEqual(run['output']['operation_status'],'unavailable')

    def test_changed_implementation_cannot_claim_old_replay(self):
        run = run_snapshot(self.settings,self.snapshot()['snapshot_id'])
        with patch('backend.analysis.research_pipeline.implementation_hash',return_value='different'), self.assertRaises(AppError) as error:
            replay_run(self.settings,run['run_id'])
        self.assertEqual(error.exception.status_code,409)

    def test_same_benchmark_pareto_and_share_classes_not_diversification(self):
        output = self.compare()
        items = {item['code']:item for item in output['items']}
        self.assertEqual(items['510001']['status'],'research_candidate')
        self.assertEqual(items['510002']['status'],'dominated')
        self.assertEqual(items['510002']['dominated_by'],['510001'])
        self.assertEqual(output['recommendation_status'],'unvalidated')
        self.assertTrue(output['share_class_groups'])
        self.assertEqual(Decimal(items['510001']['metrics']['tracking_difference_pct']),0)
        self.assertEqual(Decimal(items['510001']['metrics']['tracking_error_annualized_pct']),0)

    def test_no_fee_deducted_again_from_net_total_return(self):
        output = self.compare()
        for item in output['items']:
            self.assertEqual(Decimal(item['metrics']['tracking_difference_pct']),0)
        self.assertEqual(output['cost_comparison'],'annual_terms_only_not_investor_total_cost')

    def test_equal_terms_preserve_tie_instead_of_code_based_winner(self):
        facts = full_facts()
        for fact in facts:
            if fact['kind']=='fund_profile': fact['annual_fee_bps']='15'
        output = self.compare(facts)
        self.assertEqual([item['status'] for item in output['items']],['research_candidate']*2)

    def test_unknown_fee_suspended_status_and_old_profile_not_eligible(self):
        for fields in ({'annual_fee_bps':None},{'subscription_status':'suspended'},{'subscription_status':'limited'},{'effective_date':'2026-09-15'}):
            with self.subTest(fields=fields):
                facts = [fact for fact in full_facts() if fact['subject_key'] != FUND_B]
                for fact in facts:
                    if fact['kind']=='fund_profile': fact.update(fields)
                output = self.compare(facts)
                item = next(item for item in output['items'] if item['code']=='510001')
                self.assertEqual(item['status'],'insufficient')
                self.assertTrue(item['reasons'])
                # The old dated profile may coexist with a newer test fact: isolate subcases.
                self.tearDown_database()

    def tearDown_database(self):
        self.temp.cleanup()
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name)/'db.sqlite3')

    def test_active_and_feeder_not_ranked_as_passive(self):
        facts = full_facts()
        for fact in facts:
            if fact['kind']=='fund_profile': fact['category']='active' if fact['fund_code']=='510001' else 'feeder'
        self.assertTrue(all(item['status']=='excluded' for item in self.compare(facts)['items']))

    def test_unit_nav_not_total_return_and_shared_missing_day_block(self):
        for modification in ('nav_only','missing_day'):
            with self.subTest(modification=modification):
                facts = full_facts()
                if modification=='nav_only':
                    for fact in facts:
                        if fact['subject_key'] == FUND_A and fact['kind']=='series':
                            fact.update(series_kind='nav',basis='unit_nav')
                else:
                    facts = [fact for fact in facts if not (fact['subject_key'] in (FUND_A,SUBJECT) and fact['kind']=='series' and fact['effective_date']=='2026-09-10')]
                output = self.compare(facts)
                item = next(item for item in output['items'] if item['code']=='510001')
                self.assertEqual(item['status'],'insufficient')
                self.tearDown_database()

    def test_etf_needs_same_day_nav_and_full_liquidity_series(self):
        facts = [fact for fact in full_facts() if fact['subject_key']!=FUND_B]
        for fact in facts:
            if fact['kind']=='fund_profile': fact['category']='passive_etf'
        output = self.compare(facts)
        self.assertEqual(output['items'][0]['status'],'insufficient')
        extra = series(FUND_A,kind='price',count=20) + series(FUND_A,kind='nav',count=1)
        output = self.compare(facts+extra)
        self.assertEqual(output['items'][0]['status'],'research_candidate')
        self.assertIn('premium_pct', output['items'][0]['metrics'])

    def test_current_industry_background_does_not_replace_index_profit(self):
        facts = full_facts()
        for fact in facts:
            if fact['kind']=='numeric' and fact['metric']!='pe_ttm':
                fact.update(scope='broad_industry',semantic_status='background')
        output = assess_subject(self.snapshot(facts)['manifest'],SUBJECT)
        self.assertTrue(output['background_revision_ids'])
        # Background is still excluded from earnings; valid prices may describe the short horizon.
        self.assertEqual(output['operating'], {})
        self.assertTrue(all(period['state']=='insufficient' for period in output['periods'] if period['id']!='short'))
        self.assertEqual(output['periods'][0]['assessment']['basis'], 'price_and_risk')
        self.assertEqual(output['periods'][0]['assessment']['recommendation_status'], 'not_evaluated')

    def test_negative_profit_retains_risk_even_with_other_missing_inputs(self):
        facts = [numeric(value='10',effective='2026-06-30'),numeric(value='-10')]
        output = assess_subject(self.snapshot(facts)['manifest'],SUBJECT)
        self.assertTrue(all(period['state']=='risk' for period in output['periods'] if period['id']!='short'))
        self.assertEqual(output['periods'][0]['state'], 'insufficient')  # no price observations
        self.assertTrue(any('归母净利润同比' in value for value in output['periods'][0]['assessment']['challenges']))
        self.assertTrue(all(period['gaps'] for period in output['periods']))

    def test_valuation_single_date_or_nonpositive_history_never_gets_rank(self):
        for kind in ('one','nonpositive'):
            facts = full_facts()
            if kind=='one': facts = [fact for fact in facts if fact.get('metric')!='pe_ttm' or fact['effective_date']=='2026-09-16']
            else:
                next(fact for fact in facts if fact.get('metric')=='pe_ttm')['value']='-5'
            output = assess_subject(self.snapshot(facts)['manifest'],SUBJECT)
            self.assertIsNone(output['valuation']['percentile'])
            self.assertTrue(output['valuation']['gaps'])
            self.tearDown_database()

    def test_valuation_minimum_span_and_count_are_explicit(self):
        output = assess_subject(self.snapshot()['manifest'],SUBJECT)
        self.assertGreaterEqual(output['valuation']['span_days'],365)
        self.assertGreaterEqual(output['valuation']['sample_count'],60)
        self.assertIsNotNone(output['valuation']['percentile'])
        self.assertIn('不是上涨概率',output['valuation']['interpretation'])

    def test_quality_and_source_conflict_cannot_fallback_to_old_pool(self):
        snapshot = self.snapshot()
        point = next(f for f in snapshot['manifest']['facts'] if f['subject_key']==SUBJECT and f['dataset_kind']=='numeric' and f['metric']=='pe_ttm')
        quarantine(self.settings,point['revision_id'],'fixture wrong source')
        current = freeze_snapshot(self.settings,[SUBJECT,BENCHMARK,FUND_A,FUND_B])
        assessment = assess_subject(current['manifest'],SUBJECT)
        self.assertTrue(assessment['quality_blocked'])
        self.assertFalse(assessment['evidence_context']['usable'])
        self.assertIsNone(assessment['evidence_context']['valuation']['percentile'])
        self.assertTrue(all(period['state']=='insufficient' for period in assessment['periods'] if period['id']!='short'))
        self.assertTrue(all(period['recommendation_status']=='unvalidated' for period in assessment['periods']))
        output = compare_passive_funds(current['manifest'],SUBJECT)
        self.assertTrue(all(item['status']=='insufficient' for item in output['items']))

    def test_tracking_error_uses_sample_active_return_formula(self):
        fund = [{'effective_date':str(i),'value':x} for i,x in enumerate(('100','110','110'))]
        base = [{'effective_date':str(i),'value':x} for i,x in enumerate(('100','100','100'))]
        result = _tracking(fund,base)
        expected = Decimal('0.005').sqrt()*Decimal(252).sqrt()*100
        self.assertEqual(Decimal(result['tracking_error_annualized_pct']),expected)
        self.assertEqual(Decimal(result['tracking_difference_pct']),10)
