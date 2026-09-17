"""Deterministic regression only. Synthetic prices are not live-provider evidence."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from asgi_client import ASGITestClient as TestClient

from backend.analysis.sector_status import analyze_index, sector_opportunities
from backend.analysis.sector_strength import attach_strength
from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.integrations.market_refresh import refresh_market_data
from backend.storage.catalog import import_catalog
from backend.storage.related_market import import_related_index, record_relation_check
from backend.storage.sector_heat import read_sector_heat, save_sector_heat
from scripts.refresh_market_data import collect

NOW = datetime(2026, 9, 16, 17, tzinfo=ZoneInfo('Asia/Shanghai'))


def prices(last='110'):
    days, day = [], NOW.date()
    while len(days) < 121:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    result = [{'date': day, 'open': '100', 'high': '2000', 'low': '90',
               'close': '100', 'volume': '100', 'amount': '10000'} for day in reversed(days)]
    result[-1]['close'] = last
    return result


class BrowseReassessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / 'app.sqlite3')
        import_catalog(self.settings, [{'code': f'{n:06}', 'name': f'测试基金{n}', 'fund_type': 'ETF'}
                                       for n in range(1, 18)], policy_version='fixture')
        self.client = self.enterContext(TestClient(create_app(self.settings)))
        clock = self.enterContext(patch('backend.analysis.sector_status.datetime'))
        clock.now.return_value = NOW

    def publish(self, close='110'):
        save_sector_heat(self.settings, as_of='2026-09-16', updated_at=NOW.isoformat(),
                         catalog_count=1, requested_count=1, universe_scope='verified_industry_all',
                         members=[{'code': 'BK0001', 'name': '测试行业', 'kind': '行业', 'heat_rank': 1,
                                   'heat_value': '10000', 'heat_updated_at': NOW.isoformat(),
                                   'updated_at': NOW.isoformat(), 'collection_error': None, 'rows': prices(close),
                                   'constituents': [], 'constituent_error': None}])

    @staticmethod
    def worker(status='success', returncode=0):
        return subprocess.CompletedProcess([], returncode, json.dumps({
            'target': 'sectors', 'status': status, 'message': 'synthetic collector outcome'}), '')

    def test_empty_history_does_not_return_the_catalogue(self):
        result = self.client.get('/api/funds?codes=&page_size=6').json()
        self.assertEqual((result['items'], result['total'], result['catalog_total'], result['selection_mode']),
                         ([], 0, 17, 'codes'))
        self.assertEqual(self.client.get('/api/funds').json()['total'], 17)

    def test_history_order_deduplication_missing_codes_and_both_page_sizes(self):
        codes = [f'{n:06}' for n in range(17, 0, -1)]
        value = ','.join(['999999', codes[0], *codes])
        for size in (6, 8):
            seen = []
            for page in range(1, 1 + (17 + size - 1) // size):
                result = self.client.get('/api/funds', params={'codes': value, 'page': page, 'page_size': size}).json()
                self.assertEqual(result['total'], 17)
                self.assertLessEqual(len(result['items']), size)
                seen.extend(item['code'] for item in result['items'])
            self.assertEqual(seen, codes)
        self.assertEqual(self.client.get('/api/funds?codes=000001&page=99').json()['items'], [])

    def test_history_input_is_bounded_and_never_sql(self):
        for value in ['1', '000001,', '１２３４５６', "000001' OR 1=1", ','.join(['000001'] * 61)]:
            with self.subTest(value=value):
                self.assertEqual(self.client.get('/api/funds', params={'codes': value}).status_code, 422)
        self.assertEqual(self.client.get('/api/funds?codes=000001&q=测试').status_code, 422)

    def test_browsed_cards_reload_identity_and_current_relation(self):
        import_related_index(self.settings, fund_code='000001', index_code='980017', index_name='测试指数',
                             rows=prices(), source_id='fixture.index', relation_source_id='fixture.prospectus',
                             evidence_url='https://example.test/prospectus')
        self.assertEqual(self.client.get('/api/funds?codes=000001').json()['items'][0]['relation_status'], 'linked')
        record_relation_check(self.settings, '000001', outcome='failed', error='synthetic failure')
        import_catalog(self.settings, [{'code': '000001', 'name': '更名后的基金', 'fund_type': 'ETF'}], policy_version='fixture')
        item = self.client.get('/api/funds?codes=000001').json()['items'][0]
        self.assertEqual((item['name'], item['relation_status'], item['related_sectors']), ('更名后的基金', 'withheld', []))

    def test_health_advertises_reassessment_contract(self):
        result = self.client.get('/health').json()
        self.assertEqual(result['api_contract'], 'market-workbench-v2')
        self.assertIn('sector_refresh_then_evaluate', result['capabilities'])

    def test_five_http_reassessments_each_publish_then_compute_new_prices(self):
        sequence = iter(range(111, 116))
        def run(*args, **kwargs):
            self.publish(str(next(sequence)))
            return self.worker()
        with patch('backend.integrations.market_refresh.subprocess.run', side_effect=run) as worker:
            values = []
            for expected in range(11, 16):
                response = self.client.post('/api/sectors/refresh')
                self.assertEqual(response.status_code, 200)
                result = response.json()
                self.assertEqual(result['refresh']['status'], 'success')
                self.assertEqual(result['api_contract'], 'market-workbench-v2')
                self.assertEqual(result['evaluation']['items'][0]['periods'][0]['return_pct'], expected)
                values.append(result['evaluation']['items'][0]['periods'][0]['return_pct'])
            self.assertEqual(values, [11, 12, 13, 14, 15])
            self.assertEqual(worker.call_count, 5)

    def test_success_failure_timeout_and_recovery_in_seven_http_calls(self):
        outcomes = [200, 502, 200, 504, 200, 502, 200]
        for status in outcomes:
            if status == 200:
                def run(*args, **kwargs):
                    self.publish()
                    return self.worker()
                mock = patch('backend.integrations.market_refresh.subprocess.run', side_effect=run)
            elif status == 504:
                mock = patch('backend.integrations.market_refresh.subprocess.run', side_effect=subprocess.TimeoutExpired('fixture', 600))
            else:
                mock = patch('backend.integrations.market_refresh.subprocess.run', return_value=self.worker('failed', 2))
            with self.subTest(status=status), mock:
                response = self.client.post('/api/sectors/refresh')
                self.assertEqual(response.status_code, status)
                current = self.client.get('/api/sectors/opportunities').json()
                self.assertEqual(current['items'][0]['periods'][0]['strength']['eligible'], status == 200)
                self.assertEqual(read_sector_heat(self.settings)['items'][0]['rows'][-1]['close'], '110')
                if status != 200:
                    self.assertNotIn('evaluation', response.json())
                    self.assertTrue(current['universe']['last_error'])

    def test_lock_covers_evaluation_not_only_network_import(self):
        self.publish()
        def evaluate(settings):
            with self.assertRaises(AppError) as caught:
                refresh_market_data(settings, 'sectors')
            self.assertEqual(caught.exception.status_code, 409)
            return sector_opportunities(settings)
        with patch('backend.integrations.market_refresh.subprocess.run', return_value=self.worker()) as worker, \
             patch('backend.integrations.market_refresh.sector_opportunities', side_effect=evaluate):
            self.assertEqual(self.client.post('/api/sectors/refresh').status_code, 200)
        self.assertEqual(worker.call_count, 1)

    def test_evaluation_failure_is_not_reported_as_collection_and_evaluation_success(self):
        self.publish()
        with patch('backend.integrations.market_refresh.subprocess.run', return_value=self.worker()), \
             patch('backend.integrations.market_refresh.sector_opportunities', side_effect=ValueError('fixture')):
            result = self.client.post('/api/sectors/refresh')
        self.assertEqual(result.status_code, 500)
        self.assertEqual(result.json()['error']['code'], 'market_evaluation_failed')
        with patch('backend.integrations.market_refresh.subprocess.run', return_value=self.worker()):
            self.assertEqual(self.client.post('/api/sectors/refresh').status_code, 200)

    def test_worker_cannot_shrink_coverage_denominator_to_claim_success(self):
        fixture = {'universe': {'ranking_as_of': '2026-09-16', 'requested_count': 3, 'catalog_count': 5},
                   'items': [{'code': 'BK0001', 'rows': prices(), 'collection_error': None, 'membership': {'status': 'ready'}}]}
        with patch('scripts.refresh_market_data.refresh_sector_heat', return_value=fixture):
            result = collect(self.settings, 'sectors')
        self.assertEqual((result['status'], result['requested_count'], result['price_failed'], result['identity_excluded_count']),
                         ('partial', 3, 2, 2))
        self.assertFalse(result['manual_evidence_refreshed'])

    def test_intraday_bar_does_not_enter_price_ranking(self):
        self.publish('1000')
        with patch('backend.analysis.sector_status.datetime') as clock:
            clock.now.return_value = NOW.replace(hour=15)
            result = sector_opportunities(self.settings)
        self.assertEqual(result['price_cutoff'], '2026-09-15')
        self.assertEqual(result['items'][0]['as_of'], '2026-09-15')
        self.assertEqual(result['items'][0]['periods'][0]['return_pct'], 0)
        self.assertEqual(result['calendar_status'], 'ready')

    def test_display_rounding_does_not_create_a_ranking_tie(self):
        items = []
        for code, close in [('a', '101.0001'), ('b', '101.0002')]:
            item = analyze_index(prices(close), as_of=NOW.date())
            items.append({**item, 'code': code, 'universe_type': 'hot_board'})
        attach_strength(items, '2026-09-16')
        self.assertEqual([item['periods'][0]['return_pct'] for item in items], [1, 1])
        self.assertEqual([item['periods'][0]['strength']['rank'] for item in items], [2, 1])

    def test_equal_endpoints_do_not_hide_missing_internal_dates(self):
        items = []
        for code in ('a', 'b', 'c'):
            rows = prices()
            if code == 'c':
                # Keep count, first and last unchanged, but replace an internal observation date.
                rows[-10]['date'] = '2026-09-05'
            items.append({**analyze_index(rows, as_of=NOW.date()), 'code': code, 'universe_type': 'hot_board'})
        attach_strength(items, '2026-09-16')
        self.assertEqual([item['periods'][0]['strength']['eligible'] for item in items], [True, True, False])
        self.assertIn('内部观测日期', items[-1]['periods'][0]['strength']['reason'])


if __name__ == '__main__':
    unittest.main()
