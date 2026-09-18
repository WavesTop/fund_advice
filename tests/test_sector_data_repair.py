"""Regression cases derived from the uploaded diagnostics; upstream values are synthetic."""
import copy
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.analysis.sector_status import analyze_index, sector_opportunities
from backend.analysis.sector_strength import attach_strength
from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.trading_calendar import SHANGHAI, get_calendar
from backend.integrations.sector_financials import collect_sector_fundamentals, fetch_table, parse_rows
from backend.storage.collection_runs import start_run, finish_run, record_progress, latest_sector_status
from backend.storage.database import migrate, connection_scope
from backend.storage.research import utc_now
from backend.storage.sector_fundamentals import known_observations
from backend.storage.sector_heat import save_sector_heat, read_sector_heat
from scripts.import_sector_heat import fetch_sector_constituents, fetch_ths_sector_daily, fetch_sector_daily
from sector_financial_fixtures import SourceFixture, board, NOW, SUBJECT


class DataRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name)/'db.sqlite3')
        migrate(self.settings)

    def constituent_source(self, cap):
        rows = [{'f12': '600547', 'f13': 1, 'f14': '合成公司1', 'f20': cap},
                {'f12': '600489', 'f13': 1, 'f14': '合成公司2', 'f20': '123.45'}]
        return lambda *_: {'rc': 0, 'data': {'total': 2, 'diff': copy.deepcopy(rows)}}

    def test_missing_optional_caps_preserve_every_security(self):
        for cap in (None, '-', '--', '', 'NaN', '-1', True, {}):
            with self.subTest(cap=cap):
                rows = fetch_sector_constituents(board(), self.constituent_source(cap))
                self.assertEqual([r['stock_code'] for r in rows], ['600547', '600489'])
                self.assertIsNone(rows[0]['market_cap'])
                self.assertEqual(rows[1]['market_cap'], '123.45')

    def test_valid_cap_keeps_exact_decimal(self):
        rows = fetch_sector_constituents(board(), self.constituent_source('12345678901234567890.01'))
        self.assertEqual(rows[0]['market_cap'], '12345678901234567890.01')

    def test_missing_identity_still_blocks_membership(self):
        payload = self.constituent_source('-')('', 1)
        payload['data']['diff'][0]['f12'] = 'unknown'
        with self.assertRaises(ValueError):
            fetch_sector_constituents(board(), lambda *_: payload)

    def test_short_page_still_blocks_membership(self):
        payload = self.constituent_source('-')('', 1)
        payload['data']['total'] = 3
        with self.assertRaises(ValueError):
            fetch_sector_constituents(board(), lambda *_: payload)

    def test_cap_null_survives_storage_and_readback(self):
        item = board()
        item['constituents'] = fetch_sector_constituents(item, self.constituent_source('-'))
        save_sector_heat(self.settings, as_of='2026-09-16', updated_at='2026-09-16T08:00:00Z',
                         catalog_count=1, members=[item], requested_count=1, universe_scope='verified_industry_all')
        membership = read_sector_heat(self.settings)['items'][0]['membership']
        self.assertEqual(membership['status'], 'ready')
        self.assertEqual(membership['member_count'], 2)
        self.assertEqual(membership['market_cap_missing_count'], 1)
        self.assertIsNone(membership['members'][0]['market_cap'])

    def ths(self, rows):
        item = {**board(), 'ranking_as_of': '2026-09-17', 'price_cutoff': '2026-09-16'}
        body = 'callback(' + json.dumps({'name': item['name'], 'data': ';'.join(rows)}) + ')'
        with patch('scripts.import_sector_heat.fetch_text', return_value=body):
            return fetch_ths_sector_daily(item, '881001')

    def test_pending_bar_is_excluded_before_numeric_validation(self):
        rows = self.ths(['20260916,100,102,99,101,200,1000', '20260917,-,-,-,-,-,-'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['date'], '2026-09-16')

    def test_bad_completed_bar_reports_identity_date_and_raw_value(self):
        with self.assertRaisesRegex(ValueError, r'BK0732.*2026-09-16.*open.*bad'):
            self.ths(['20260916,bad,102,99,101,200,1000'])

    def test_duplicate_completed_bar_is_not_silently_skipped(self):
        row = '20260916,100,102,99,101,200,1000'
        with self.assertRaisesRegex(ValueError, '日期重复'):
            self.ths([row, row])

    def test_completed_short_record_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '字段不完整'):
            self.ths(['20260916,100'])

    def test_native_history_requests_completed_end(self):
        urls = []
        item = {**board(), 'ranking_as_of': '2026-09-17', 'price_cutoff': '2026-09-16'}
        def source(url, timeout):
            urls.append(url)
            return {'rc': 0, 'data': {'code': item['code'], 'market': 90, 'name': item['name'],
                                     'klines': ['2026-09-16,100,101,102,99,200,1000']}}
        self.assertEqual(fetch_sector_daily(item, source)[0]['date'], '2026-09-16')
        self.assertIn('end=20260916', urls[0])

    def test_stale_display_has_metrics_but_no_current_eligibility(self):
        result = analyze_index(board()['rows'], as_of=date(2026, 9, 17), calendar=get_calendar())
        for period in result['periods']:
            self.assertEqual(period['status'], 'insufficient')
            self.assertIsNone(period['return_pct'])
            self.assertTrue(period['historical']['available'])
            self.assertEqual(period['historical']['as_of'], '2026-09-16')
            self.assertIsInstance(period['historical']['return_pct'], float)
            self.assertNotIn('_ranking_return', period['historical'])
        items = [{**result, 'universe_type': 'hot_board', 'code': 'BK0732'}]
        attach_strength(items, {'hot_board': '2026-09-17'})
        self.assertTrue(all(not p['strength']['eligible'] for p in items[0]['periods']))

    def test_internal_gap_is_not_concealed_as_a_historical_sample(self):
        rows = board()['rows']; rows.pop(-5)
        result = analyze_index(rows, as_of=date(2026, 9, 17), calendar=get_calendar())
        self.assertTrue(all('historical' not in p for p in result['periods']))

    def test_invalid_historical_price_is_not_displayed(self):
        rows = board()['rows']; rows[-1]['close'] = 'NaN'
        result = analyze_index(rows, as_of=date(2026, 9, 17), calendar=get_calendar())
        self.assertTrue(all('historical' not in p for p in result['periods']))

    def test_invalid_dates_are_not_repaired_by_historical_fallback(self):
        rows = board()['rows']; rows.append(rows[-1])
        result = analyze_index(rows, as_of=date(2026, 9, 17), calendar=get_calendar())
        self.assertTrue(all('historical' not in p for p in result['periods']))

    def test_current_prices_do_not_need_historical_fallback(self):
        result = analyze_index(board()['rows'], as_of=date(2026, 9, 16), calendar=get_calendar())
        self.assertTrue(all(p['status'] == 'strong' and 'historical' not in p for p in result['periods']))

    def test_bad_issuer_number_does_not_discard_other_issuers(self):
        rows = [{'SECURITY_CODE': '600547', 'REPORT_DATE': '2026-06-30', 'NOTICE_DATE': '2026-08-10',
                 'TOTAL_OPERATE_INCOME': 'bad', 'PARENT_NETPROFIT': '10'},
                {'SECURITY_CODE': '600489', 'REPORT_DATE': '2026-06-30', 'NOTICE_DATE': '2026-08-10',
                 'TOTAL_OPERATE_INCOME': '100', 'PARENT_NETPROFIT': '20'}]
        valid, excluded = parse_rows(rows, 'income', '2026-06-30', NOW)
        self.assertEqual(set(valid), {'600489'})
        self.assertIn('数值字段无效', excluded['600547'])

    def test_missing_required_source_columns_still_rejects_schema(self):
        rows = [{'SECURITY_CODE': '600547', 'REPORT_DATE': '2026-06-30', 'NOTICE_DATE': '2026-08-10'}]
        with self.assertRaisesRegex(ValueError, '来源字段发生变化'):
            parse_rows(rows, 'income', '2026-06-30', NOW)

    def test_source_error_retains_code_and_message(self):
        raw = json.dumps({'success': False, 'code': 9501, 'message': 'Unknown column SAMPLE'}).encode()
        with self.assertRaisesRegex(ValueError, '9501.*Unknown column SAMPLE'):
            fetch_table(self.settings, 'income', '2026-06-30', NOW, fetcher=lambda *_: raw,
                        deadline=1e30)

    def test_supported_scope_does_not_shrink_whole_denominator(self):
        item = board();item['membership']['members'].append(
            {'stock_code': '200468', 'stock_name': '合成范围外公司', 'market': 0, 'source_order': 3})
        item['membership']['member_count'] = 3
        result = collect_sector_fundamentals(self.settings, [item], fetcher=SourceFixture(), now=NOW)
        self.assertEqual(result['status'], 'partial')
        with connection_scope(self.settings) as conn:
            value = known_observations(conn, [SUBJECT], utc_now())[SUBJECT]
        metric = value['operating']['periods'][0]['metrics']['revenue_yoy']
        self.assertEqual((metric['covered'], metric['total'], metric['supported_total']), (2, 3, 2))
        self.assertTrue(metric['supported_complete'])
        self.assertFalse(metric['complete'])
        self.assertEqual(metric['excluded'][0]['code'], '200468')

    def test_financial_stages_are_observable(self):
        events = []
        collect_sector_fundamentals(self.settings, [board()], fetcher=SourceFixture(), now=NOW, progress=events.append)
        self.assertEqual(len(events), 18)
        self.assertEqual(sum(e['state'] == 'running' for e in events), 9)
        self.assertTrue(all(e['stage'] == 'financials' and e['business_date'] for e in events))

    def test_durable_progress_and_database_identity(self):
        run = start_run(self.settings, 'sectors')
        record_progress(self.settings, run, {'stage': 'financials', 'dataset': 'income', 'state': 'running'})
        reloaded = Settings(database_path=self.settings.database_path)
        state = latest_sector_status(reloaded)
        self.assertEqual(state['run_id'], run)
        self.assertEqual(state['result']['progress']['stage'], 'financials')
        finish_run(self.settings, run, 'partial', {'message': 'synthetic'})
        record_progress(self.settings, run, {'stage': 'late'})
        self.assertEqual(latest_sector_status(reloaded)['state'], 'partial')
        self.assertEqual(latest_sector_status(reloaded)['database_id'], state['database_id'])

    def test_reentering_reads_saved_rows_without_starting_collection(self):
        item = board();item['constituents'] = item['membership']['members']
        save_sector_heat(self.settings, as_of='2026-09-16', updated_at='2026-09-16T08:00:00Z',
                         catalog_count=1, members=[item], requested_count=1, universe_scope='verified_industry_all')
        app = create_app(self.settings)
        get = next(route.endpoint for route in app.routes if route.path == '/api/sectors/opportunities')
        with patch('backend.api.main.refresh_market_data') as refresh:
            results = [get() for _ in range(5)]
        refresh.assert_not_called()
        self.assertTrue(all(len(result['items']) == 1 for result in results))
        self.assertTrue(all(result['items'][0]['observation_count'] == len(item['rows']) for result in results))
        self.assertEqual(latest_sector_status(self.settings)['state'], 'not_started')

    def test_status_route_precedes_dynamic_sector_route(self):
        paths = [route.path for route in create_app(self.settings).routes]
        self.assertLess(paths.index('/api/sectors/collection-status'), paths.index('/api/sectors/{code}'))


    def test_failed_terminal_state_retains_last_attempted_phase(self):
        run_id = start_run(self.settings, 'sectors')
        record_progress(self.settings, run_id, {'stage': 'financials', 'dataset': 'income', 'state': 'running'})
        finish_run(self.settings, run_id, 'timeout', {'message': 'synthetic timeout'})
        current = latest_sector_status(self.settings)
        self.assertEqual(current['state'], 'timeout')
        self.assertEqual(current['result']['last_progress']['dataset'], 'income')
        self.assertEqual(current['result']['message'], 'synthetic timeout')

    def test_refresh_response_exposes_terminal_state_not_running(self):
        from backend.integrations.market_refresh import refresh_market_data
        from subprocess import CompletedProcess
        worker = {'target': 'sectors', 'status': 'partial', 'message': 'synthetic partial'}
        with patch('backend.integrations.market_refresh.subprocess.run', return_value=CompletedProcess([], 0, json.dumps(worker), '')):
            response = refresh_market_data(self.settings, 'sectors')
        status = response['evaluation']['collection_status']
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(status['run_id'], response['collection_run_id'])
        self.assertIsNotNone(status['finished_at'])

    def test_completed_response_can_lookup_its_own_run_after_next_one_starts(self):
        first = start_run(self.settings, 'sectors')
        finish_run(self.settings, first, 'partial', {'message': 'first'})
        second = start_run(self.settings, 'sectors')
        self.assertEqual(latest_sector_status(self.settings)['run_id'], second)
        own = latest_sector_status(self.settings, run_id=first)
        self.assertEqual(own['run_id'], first)
        self.assertEqual(own['state'], 'partial')

if __name__ == '__main__':
    unittest.main()
