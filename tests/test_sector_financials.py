import copy
from datetime import date, timedelta, timezone
from itertools import count
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from asgi_client import ASGITestClient
from backend.api.main import create_app
from backend.analysis.research_pipeline import get_run, replay_run, run_snapshot
from backend.analysis.sector_assessment import context_from_bundle, evaluate_period
from backend.analysis.sector_status import analyze_index, sector_opportunities
from backend.core.config import Settings
from backend.core.trading_calendar import get_calendar
from backend.integrations.sector_financials import (collect_sector_fundamentals, fetch_table, operating_sample,
    parse_rows, report_periods, previous_year)
from backend.storage.database import connection_scope, migrate
from backend.storage.research import freeze_snapshot, utc_now
from backend.storage.sector_fundamentals import known_observations, membership_identity, save_observation
from backend.storage.sector_heat import read_sector_heat, save_sector_heat
from scripts.refresh_market_data import collect
from sector_financial_fixtures import NOW, SUBJECT, SourceFixture, board, FrozenDatetime


class SectorFinancialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name)/"db.sqlite3")
        migrate(self.settings)
        # Freeze market and system clocks together. Observation instants still advance
        # so a snapshot before collection cannot see subsequently stored evidence.
        ticks = count()
        def known_now():
            value = NOW - timedelta(seconds=30) + timedelta(microseconds=next(ticks))
            return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        for target in ("backend.storage.research.utc_now", "backend.storage.sector_fundamentals.utc_now",
                       "backend.integrations.research_inputs.utc_now", "backend.analysis.research_pipeline.utc_now",
                       "backend.storage.collection_runs.utc_now", "backend.integrations.sector_financials.utc_now",
                       __name__ + ".utc_now"):
            system_clock = patch(target, known_now)
            system_clock.start()
            self.addCleanup(system_clock.stop)
        for target in ("backend.analysis.sector_status.datetime", "backend.integrations.sector_financials.datetime"):
            clock_patch = patch(target, FrozenDatetime)
            clock_patch.start()
            self.addCleanup(clock_patch.stop)

    def read(self):
        with connection_scope(self.settings) as conn:
            return known_observations(conn, [SUBJECT], utc_now())[SUBJECT]

    def gather(self, fixture=None, item=None):
        return collect_sector_fundamentals(self.settings, [item or board()], fetcher=fixture or SourceFixture(), now=NOW)

    def publish_prices(self):
        b = board()
        b['constituents'] = b['membership']['members']
        save_sector_heat(self.settings, as_of="2026-09-16", updated_at="2026-09-16T08:10:00Z",
                         catalog_count=1, members=[b], requested_count=1, universe_scope="verified_industry_all")
        return read_sector_heat(self.settings)

    def period_results(self, item=None):
        item = item or board()
        context = context_from_bundle(self.read(), NOW, board=item)
        periods = analyze_index(item['rows'], as_of=get_calendar().latest_completed(NOW))['periods']
        return [evaluate_period(p, context) for p in periods]

    def test_latest_ended_quarter_is_not_silently_replaced_by_old_complete_period(self):
        self.assertEqual(report_periods(date(2026, 9, 17)), ['2026-06-30', '2026-03-31'])
        self.assertEqual(report_periods(date(2026, 10, 1)), ['2026-09-30', '2026-06-30'])
        self.assertEqual(report_periods(date(2026, 6, 30)), ['2026-03-31', '2025-12-31'])
        self.assertEqual(previous_year('2026-03-31'), '2025-03-31')

    def test_collects_income_cashflow_and_dated_valuation_and_archives_originals(self):
        source = SourceFixture()
        self.assertEqual(self.gather(source)['status'], 'success')
        self.assertEqual(len(source.calls), 9)
        result = self.read()
        metric = result['operating']['periods'][0]['metrics']['profit_yoy']
        self.assertEqual(Decimal(metric['value']), 50)
        self.assertEqual((metric['covered'], metric['total']), (2, 2))
        self.assertEqual(result['valuation']['median_pe_ttm'], '25')
        self.assertEqual(result['valuation']['as_of'], '2026-09-16')
        with connection_scope(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM raw_asset').fetchone()[0], 9)
            for source in result['sources']:
                raw = conn.execute('SELECT body FROM raw_asset WHERE asset_id=?', (source['asset_id'],)).fetchone()[0]
                self.assertTrue(json.loads(raw)['success'])

    def test_one_bulk_table_read_shared_by_multiple_boards(self):
        source = SourceFixture()
        second = board(); second.update(code='BK1234', name='合成其他行业')
        result = collect_sector_fundamentals(self.settings, [board(), second], fetcher=source, now=NOW)
        self.assertEqual(result['available'], 2)
        self.assertEqual(len(source.calls), 9)  # no repeated per-board/per-company scraping

    def test_aggregate_yoy_is_ratio_of_sums_not_average_of_company_growth(self):
        def change(report, d, rows):
            if report == 'RPT_DMSK_FN_INCOME':
                rows[0]['TOTAL_OPERATE_INCOME'] = '1000' if d.startswith('2025') else '1100'
                rows[1]['TOTAL_OPERATE_INCOME'] = '100' if d.startswith('2025') else '200'
            return rows
        self.gather(SourceFixture(change))
        value = Decimal(self.read()['operating']['periods'][0]['metrics']['revenue_yoy']['value'])
        self.assertEqual(value, (Decimal(1300)/1100-1)*100)
        self.assertNotEqual(value, Decimal('55'))

    def test_partial_disclosure_keeps_full_denominator_and_excluded_company(self):
        def change(report, d, rows):
            if report == 'RPT_DMSK_FN_INCOME' and d == '2026-06-30': rows[0]['PARENT_NETPROFIT'] = None
            return rows
        self.assertEqual(self.gather(SourceFixture(change))['status'], 'partial')
        metrics = self.read()['operating']['periods'][0]['metrics']
        for metric in ('revenue_yoy','profit_yoy'):
            self.assertEqual((metrics[metric]['covered'], metrics[metric]['total']), (1, 2))
            self.assertFalse(metrics[metric]['complete'])
            self.assertEqual(metrics[metric]['excluded'][0]['code'], '600547')
        self.assertEqual(self.period_results()[1]['label'], '中期业绩待确认')

    def test_negative_baseline_never_produces_misleading_growth_percentage(self):
        def change(report, d, rows):
            if report == 'RPT_DMSK_FN_INCOME':
                for row in rows: row['PARENT_NETPROFIT'] = '-10' if d.startswith('2025') else '15'
            return rows
        self.gather(SourceFixture(change))
        metric = self.read()['operating']['periods'][0]['metrics']['profit_yoy']
        self.assertIsNone(metric['value']); self.assertEqual(metric['baseline'], 'nonpositive')
        self.assertEqual(Decimal(metric['delta']), 50)
        self.assertNotEqual(self.period_results()[1]['status'], 'watch')

    def test_zero_baseline_never_divides_by_zero_or_fills_zero_growth(self):
        def change(report, d, rows):
            if report == 'RPT_DMSK_FN_CASHFLOW' and d.startswith('2025'):
                for row in rows: row['NETCASH_OPERATE'] = '0'
            return rows
        self.gather(SourceFixture(change))
        metric = self.read()['operating']['periods'][0]['metrics']['operating_cashflow_yoy']
        self.assertIsNone(metric['value']); self.assertEqual(metric['current_sum'], '40')

    def test_announcement_on_current_day_not_available_until_next_midnight(self):
        def change(report, d, rows):
            if report == 'RPT_DMSK_FN_INCOME' and d == '2026-06-30':
                for row in rows: row['NOTICE_DATE'] = '2026-09-17 00:00:00'
            return rows
        self.gather(SourceFixture(change))
        metric = self.read()['operating']['periods'][0]['metrics']['profit_yoy']
        self.assertEqual(metric['covered'], 0)
        self.assertIn('保守可用时点', metric['excluded'][0]['reason'])

    def test_missing_announcement_is_not_substituted_with_fetch_time(self):
        def change(report, d, rows):
            if report == 'RPT_DMSK_FN_INCOME':
                for row in rows: row['NOTICE_DATE'] = None
            return rows
        self.gather(SourceFixture(change))
        self.assertEqual(self.read()['operating']['periods'][0]['metrics']['revenue_yoy']['covered'], 0)

    def test_duplicate_codes_rejected_not_averaged(self):
        rows = [{'SECURITY_CODE':'600547','TRADE_DATE':'2026-09-16','PE_TTM':'10'}]*2
        with self.assertRaises(ValueError): parse_rows(rows, 'valuation', '2026-09-16', NOW)

    def test_wrong_business_day_and_missing_field_rejected(self):
        for row in ({'SECURITY_CODE':'600547','TRADE_DATE':'2026-09-15','PE_TTM':'10'},
                    {'SECURITY_CODE':'600547','TRADE_DATE':'2026-09-16'}):
            with self.subTest(row=row), self.assertRaises(ValueError): parse_rows([row], 'valuation', '2026-09-16', NOW)

    def test_nonfinite_or_boolean_values_excluded_with_reason(self):
        for bad in ('NaN','Infinity',True,10.5):
            with self.subTest(bad=bad):
                valid, excluded = parse_rows([
                    {'SECURITY_CODE':'600547','TRADE_DATE':'2026-09-16','PE_TTM':bad},
                    {'SECURITY_CODE':'600489','TRADE_DATE':'2026-09-16','PE_TTM':'15'},
                ], 'valuation', '2026-09-16', NOW)
                self.assertNotIn('600547', valid)
                self.assertEqual(valid['600489']['PE_TTM'], '15')
                self.assertIn('数值字段无效', excluded['600547'])

    def test_truncated_page_is_not_a_success(self):
        source = lambda *_: json.dumps({'success':True,'result':{'count':2,'pages':1,'data':[]}}).encode()
        with self.assertRaisesRegex(ValueError,'截断'):
            fetch_table(self.settings,'income','2026-06-30',NOW,fetcher=source,deadline=10,clock=lambda:0)

    def test_page_count_drift_rejected_and_each_original_response_retained(self):
        def source(url, timeout):
            from urllib.parse import parse_qs,urlsplit
            page=int(parse_qs(urlsplit(url).query)['pageNumber'][0])
            rows=[{'SECURITY_CODE':f'{600000+i:06}','TRADE_DATE':'2026-09-16','PE_TTM':'10'} for i in range(500)]
            return json.dumps({'success':True,'result':{'count':501 if page==1 else 502,'pages':2,'data':rows if page==1 else rows[:2]}}).encode()
        with self.assertRaisesRegex(ValueError,'总数变化'):
            fetch_table(self.settings,'valuation','2026-09-16',NOW,fetcher=source,deadline=10,clock=lambda:0)
        with connection_scope(self.settings) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM raw_asset').fetchone()[0], 2)

    def test_full_pagination_reads_every_page_once(self):
        calls=[]
        def source(url, timeout):
            from urllib.parse import parse_qs,urlsplit
            page=int(parse_qs(urlsplit(url).query)['pageNumber'][0]);calls.append(page)
            start=(page-1)*500;end=min(start+500,501)
            rows=[{'SECURITY_CODE':f'{600000+i:06}','TRADE_DATE':'2026-09-16','PE_TTM':'10'} for i in range(start,end)]
            return json.dumps({'success':True,'result':{'count':501,'pages':2,'data':rows}}).encode()
        data=fetch_table(self.settings,'valuation','2026-09-16',NOW,fetcher=source,deadline=10,clock=lambda:0)
        self.assertEqual(len(data['rows']),501);self.assertEqual(calls,[1,2])

    def test_budget_exhaustion_does_not_request_another_table(self):
        calls=[]
        with self.assertRaises(TimeoutError):
            fetch_table(self.settings,'valuation','2026-09-16',NOW,fetcher=lambda *x:calls.append(x),deadline=10,clock=lambda:11)
        self.assertEqual(calls,[])

    def test_pe_negative_observations_not_silently_dropped_from_coverage(self):
        def change(report, d, rows):
            if report=='RPT_VALUEANALYSIS_DET': rows[0]['PE_TTM']='-10'
            return rows
        self.gather(SourceFixture(change));v=self.read()['valuation']
        self.assertEqual((v['covered'],v['positive_count'],v['nonpositive_count'],v['total']),(2,1,1,2))
        self.assertEqual(v['median_pe_ttm'],'30')
        self.assertNotEqual(self.period_results()[2]['status'],'watch')

    def test_valuation_failure_does_not_erase_valid_operating_data(self):
        def change(report, d, rows):
            if report=='RPT_VALUEANALYSIS_DET': raise OSError('valuation fixture offline')
            return rows
        self.assertEqual(self.gather(SourceFixture(change))['status'],'partial')
        self.assertEqual(self.read()['operating']['status'],'available')
        self.assertEqual(self.period_results()[1]['status'],'watch')
        self.assertEqual(self.period_results()[2]['label'],'经营改善，定价待验证')

    def test_three_horizons_do_not_share_one_missing_data_gate(self):
        self.gather()
        results=self.period_results()
        self.assertEqual([p['basis'] for p in results],['price_and_risk','same_member_earnings','cashflow_and_valuation'])
        self.assertEqual(len({p['label'] for p in results}),3)
        self.assertTrue(all(p['recommendation_status']=='not_evaluated' for p in results))
        self.assertFalse(any('营业总收入' in gap for gap in results[0]['missing']))

    def test_bad_earnings_remains_counterevidence_even_without_valuation(self):
        def change(report,d,rows):
            if report=='RPT_VALUEANALYSIS_DET': raise OSError('offline')
            if report=='RPT_DMSK_FN_INCOME' and d.startswith('2026'):
                for row in rows: row['PARENT_NETPROFIT']='-5'
            return rows
        self.gather(SourceFixture(change))
        self.assertEqual(self.period_results()[1]['status'],'risk')
        self.assertEqual(self.period_results()[2]['status'],'risk')
        self.assertEqual(self.period_results()[0]['status'],'watch')

    def test_financial_sector_does_not_use_industrial_cashflow_sign_rule(self):
        b=board();b['name']='银行'
        def change(report,d,rows):
            if report=='RPT_DMSK_FN_CASHFLOW' and d.startswith('2026'):
                for row in rows: row['NETCASH_OPERATE']='-40'
            return rows
        self.gather(SourceFixture(change),b)
        long=self.period_results(b)[2]
        self.assertEqual(long['label'],'金融行业专属指标待补')
        self.assertEqual(long['status'],'insufficient')

    def test_unknown_market_in_full_denominator_not_silently_removed(self):
        b=board();b['membership']['members'].append({'stock_code':'00005','stock_name':'合成港股','market':116,'source_order':3})
        b['membership']['member_count']=3
        self.gather(item=b)
        value=self.read()['operating']['periods'][0]['metrics']['profit_yoy']
        self.assertEqual((value['covered'],value['total']),(2,3))
        self.assertEqual(value['excluded'][0]['reason'],'暂未接入该市场')

    def test_stale_membership_never_triggers_financial_fetch(self):
        b=board();b['membership']['as_of']='2026-09-15'
        source=SourceFixture();self.gather(source,b)
        self.assertFalse(source.calls);self.assertEqual(self.read()['status'],'blocked')

    def test_name_similarity_or_new_source_cannot_reuse_other_board_bundle(self):
        self.gather();b=board();b['source_id']='another.source'
        context=context_from_bundle(self.read(),NOW,board=b)
        self.assertEqual(context['status'],'membership_mismatch');self.assertFalse(context['usable'])

    def test_changed_membership_invalidates_previously_full_sample(self):
        self.gather();b=board();b['membership']['members'][0]['stock_code']='600001'
        c=context_from_bundle(self.read(),NOW,board=b)
        self.assertFalse(c['usable']);self.assertIn('成分身份变化',c['errors'][0])

    def test_stale_evidence_not_counted_as_current(self):
        self.gather();c=context_from_bundle(self.read(),NOW+timedelta(days=7),board=board())
        self.assertFalse(c['usable']);self.assertNotEqual(c['valuation']['status'],'available')

    def test_running_attempt_or_failed_repeat_does_not_resurrect_prior_success(self):
        self.gather();old=self.read()
        save_observation(self.settings,{**old,'status':'collecting'})
        self.assertFalse(context_from_bundle(self.read(),NOW,board=board())['usable'])
        self.gather(SourceFixture(lambda *_: (_ for _ in ()).throw(OSError('all offline'))))
        c=context_from_bundle(self.read(),NOW,board=board())
        self.assertFalse(c['usable']);self.assertEqual(c['status'],'failed')
        self.gather();self.assertTrue(context_from_bundle(self.read(),NOW,board=board())['usable'])

    def test_snapshot_excludes_later_collection_and_pins_original_bundle(self):
        before=freeze_snapshot(self.settings,[SUBJECT])
        self.gather()
        historical=freeze_snapshot(self.settings,[SUBJECT],cutoff=before['cutoff'])
        self.assertEqual(historical['manifest']['sector_fundamentals'],{})
        current=freeze_snapshot(self.settings,[SUBJECT])
        original=current['manifest']['sector_fundamentals'][SUBJECT]['valuation']['median_pe_ttm']
        self.gather(SourceFixture(lambda r,d,rows:[{**row,'PE_TTM':'99'} for row in rows] if r=='RPT_VALUEANALYSIS_DET' else rows))
        fixed=run_snapshot(self.settings,current['snapshot_id'])
        self.assertEqual(fixed['output']['subjects'][0]['evidence_context']['valuation']['median_pe_ttm'],original)
        self.assertTrue(replay_run(self.settings,fixed['run_id'])['matches'])

    def test_evidence_storage_is_append_only(self):
        self.gather()
        with connection_scope(self.settings) as conn:
            for sql in ('DELETE FROM sector_fundamental_observation','UPDATE sector_fundamental_observation SET payload_hash="bad"'):
                with self.subTest(sql=sql),self.assertRaises(sqlite3.IntegrityError): conn.execute(sql)

    def test_same_day_retries_do_not_inflate_history_sample_count(self):
        for _ in range(5): self.gather()
        bundle=self.read();c=context_from_bundle(bundle,NOW,board=board())
        self.assertEqual(c['valuation']['history_count'],1)
        self.assertIsNone(c['valuation']['percentile'])

    def test_changed_positive_pe_cohort_does_not_splice_old_history(self):
        self.gather()
        def change(r,d,rows):
            if r=='RPT_VALUEANALYSIS_DET':rows[0]['PE_TTM']='-1'
            return rows
        self.gather(SourceFixture(change))
        self.assertEqual(self.read()['valuation_history'][0]['value'],'30')
        self.assertEqual(len(self.read()['valuation_history']),1)

    def test_get_uses_collected_facts_without_refetching_and_changes_both_page_contracts(self):
        self.publish_prices();self.gather()
        with patch('backend.integrations.sector_financials.get_bytes',side_effect=AssertionError('GET cannot scrape')):
            with ASGITestClient(create_app(self.settings)) as client:
                data=client.get('/api/sectors/opportunities').json()
                detail=client.get('/api/sectors/BK0732?source_id=sector_daily.eastmoney&universe_type=hot_board').json()
        item=data['items'][0]
        self.assertEqual(item['fundamentals']['operating']['status'],'available')
        self.assertEqual(item['periods'][1]['opportunity']['label'],'经营增长与走势相符')
        self.assertEqual(detail['item']['periods'],item['periods'])
        self.assertEqual(data['coverage'][0]['industry_current'],1)

    def test_seven_post_calls_recompute_from_each_collected_response_and_persist_snapshot(self):
        self.publish_prices()
        modes=['ok','income_fail','ok','all_fail','valuation_fail','wrong_date','ok']
        for index,mode in enumerate(modes):
            def mutate(report,d,rows):
                if mode=='all_fail' or mode=='income_fail' and report=='RPT_DMSK_FN_INCOME' or mode=='valuation_fail' and report=='RPT_VALUEANALYSIS_DET':
                    raise OSError('fixture source failure')
                if mode=='wrong_date' and report=='RPT_DMSK_FN_INCOME': rows[0]['REPORT_DATE']='2025-01-01'
                if mode=='ok' and report=='RPT_DMSK_FN_INCOME' and d.startswith('2026'):
                    for row in rows: row['PARENT_NETPROFIT']=str(15+index)
                return rows
            source=SourceFixture(mutate)
            def worker(*args,**kwargs):
                with patch('scripts.refresh_market_data.refresh_sector_heat',side_effect=lambda *_a,**_kw:self.publish_prices()), \
                     patch('backend.integrations.sector_financials.get_bytes',side_effect=lambda request,**kw:source(request.full_url,kw['timeout'])):
                    result=collect(self.settings,'sectors')
                return subprocess.CompletedProcess([],0,json.dumps(result),'')
            with self.subTest(mode=mode),patch('backend.integrations.market_refresh.subprocess.run',side_effect=worker),ASGITestClient(create_app(self.settings)) as client:
                response=client.post('/api/sectors/refresh')
                self.assertEqual(response.status_code,200)
                payload=response.json();item=payload['evaluation']['items'][0]
                self.assertEqual(payload['refresh']['status'],'success' if mode=='ok' else 'partial')
                self.assertEqual(payload['research']['status'],'recorded_unvalidated')
                fixed=get_run(self.settings,payload['research']['run_id'])
                self.assertEqual(fixed['output']['subjects'][0]['evidence_context']['observation_id'],item['fundamentals']['observation_id'])
                if mode=='ok': self.assertEqual(Decimal(item['fundamentals']['operating']['periods'][0]['metrics']['profit_yoy']['value']),Decimal(50+index*10))
                elif mode!='valuation_fail': self.assertNotEqual(item['periods'][1]['opportunity']['status'],'watch')
                self.assertEqual(len(source.calls),9)


if __name__ == '__main__':
    unittest.main()
