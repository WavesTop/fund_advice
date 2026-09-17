from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.core.trading_calendar import get_calendar, CalendarUnavailable
from backend.integrations.http_transport import get_bytes
from backend.storage.collection_runs import start_run, finish_run, record_attempt, runs
from backend.storage.database import migrate, connection_scope


class CalendarTests(unittest.TestCase):
    def test_official_long_holidays_and_makeup_workdays_are_closed(self):
        c = get_calendar()
        for raw in ('2025-01-28','2025-02-04','2025-10-08','2026-02-23','2026-09-25','2026-09-20','2026-10-07'):
            with self.subTest(raw=raw): self.assertFalse(c.is_session(date.fromisoformat(raw)))
        self.assertTrue(c.is_session(date(2026,2,24)))
        self.assertEqual(c.next_session(date(2026,2,13)), date(2026,2,24))

    def test_intraday_cutoff_and_holiday_latest_completed(self):
        c = get_calendar()
        self.assertEqual(c.latest_completed(datetime.fromisoformat('2026-09-16T15:59:59+08:00')), date(2026,9,15))
        self.assertEqual(c.latest_completed(datetime.fromisoformat('2026-09-16T16:00:00+08:00')), date(2026,9,16))
        self.assertEqual(c.latest_completed(datetime.fromisoformat('2026-10-05T18:00:00+08:00')), date(2026,9,30))

    def test_unknown_year_and_shift_never_guess_weekdays(self):
        c = get_calendar()
        with self.assertRaises(CalendarUnavailable): c.sessions(date(2024,12,30), date(2025,1,3))
        with self.assertRaises(CalendarUnavailable): c.shift(date(2026,12,31), 1)
        with self.assertRaises(ValueError): c.latest_completed(datetime(2026,9,16))

    def test_shared_missing_day_non_session_duplicate_and_order_rejected(self):
        c = get_calendar()
        self.assertIn('missing_sessions', c.validate_grid(['2026-09-14','2026-09-16'], end=date(2026,9,16))['errors'])
        for points, error in ((['2026-09-13','2026-09-14'],'non_session_dates'),
                              (['2026-09-14','2026-09-14'],'duplicate_dates'),
                              (['2026-09-15','2026-09-14'],'unordered_dates')):
            with self.subTest(points=points): self.assertIn(error, c.validate_grid(points, end=date(2026,9,16))['errors'])
        self.assertEqual(c.validate_grid([], end=date(2026,9,16))['status'], 'blocked')

    def test_contiguous_official_sessions_across_holiday_are_valid(self):
        c = get_calendar()
        dates = ['2026-02-12','2026-02-13','2026-02-24']
        self.assertEqual(c.validate_grid(dates, end=date(2026,2,24))['status'], 'ready')


class TransportTests(unittest.TestCase):
    def response(self, body=b'{"ok":true}'):
        response = BytesIO(body)
        response.status = 200
        response.headers = {'Content-Type':'application/json'}
        return response

    def test_transient_failure_retries_and_records_each_attempt(self):
        events, delays = [], []
        opener = Mock(side_effect=[URLError('DNS fixture'), HTTPError('https://example.test',503,'busy',{},None), self.response()])
        result = get_bytes(Request('https://example.test'), timeout=5, opener=opener, sleep=delays.append, clock=lambda:0, jitter=lambda:0, observe=events.append)
        self.assertEqual(result, b'{"ok":true}')
        self.assertEqual(opener.call_count, 3)
        self.assertEqual([e['attempt'] for e in events], [1,2,3])
        self.assertEqual(delays, [.25,.5])
        self.assertEqual(events[-1]['body'], result)

    def test_repeated_calls_do_not_reuse_one_success(self):
        opener = Mock(side_effect=lambda *args, **kwargs:self.response())
        for _ in range(5): get_bytes(Request('https://example.test'), timeout=5, opener=opener)
        self.assertEqual(opener.call_count, 5)

    def test_404_is_not_retried_and_schema_error_is_not_retried(self):
        opener = Mock(side_effect=HTTPError('https://example.test',404,'missing',{},None))
        with self.assertRaises(HTTPError): get_bytes(Request('https://example.test'), timeout=5, opener=opener)
        self.assertEqual(opener.call_count, 1)
        empty = Mock(side_effect=lambda *args,**kwargs:self.response(b''))
        with self.assertRaises(ValueError): get_bytes(Request('https://example.test'), timeout=5, opener=empty)
        self.assertEqual(empty.call_count, 1)

    def test_retry_after_larger_than_budget_is_not_shortened(self):
        delay = Mock()
        opener = Mock(side_effect=HTTPError('https://example.test',429,'slow',{'Retry-After':'60'},None))
        with self.assertRaises(HTTPError):
            get_bytes(Request('https://example.test'), timeout=5, opener=opener, clock=lambda:0, sleep=delay)
        self.assertEqual(opener.call_count, 1)
        delay.assert_not_called()

    def test_non_idempotent_or_bad_attempt_bound_rejected_before_io(self):
        opener = Mock()
        for request, attempts in ((Request('https://example.test', data=b'x'),3),(Request('https://example.test'),True),(Request('https://example.test'),6)):
            with self.assertRaises(ValueError): get_bytes(request,timeout=5,opener=opener,attempts=attempts)
        opener.assert_not_called()

    def test_read_exceeding_whole_budget_fails(self):
        opener = Mock(side_effect=lambda *a,**k:self.response())
        clock = Mock(side_effect=[0,0,6])
        with self.assertRaises(TimeoutError): get_bytes(Request('https://example.test'), timeout=5, attempts=1, opener=opener, clock=clock)


class DurableCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name)/'db.sqlite3')

    def test_sqlite_exclusion_does_not_depend_on_python_lock(self):
        first = start_run(self.settings,'sectors')
        with self.assertRaises(AppError) as exc: start_run(self.settings,'sectors')
        self.assertEqual(exc.exception.status_code,409)
        other = start_run(self.settings,'catalog')
        finish_run(self.settings,first,'failed',{'message':'fixture source down'})
        again = start_run(self.settings,'sectors')
        self.assertNotEqual(first, again)
        self.assertEqual(len(runs(self.settings)),3)
        with self.assertRaises(AppError): finish_run(self.settings,first,'success',{})

    def test_attempts_include_raw_response_hash_and_keep_failures(self):
        run = start_run(self.settings,'sectors')
        record_attempt(self.settings,run,{'attempt':1,'url':'https://example.test','status':503,'error':'fixture'})
        record_attempt(self.settings,run,{'attempt':2,'url':'https://example.test','status':200,'error':None,'body':b'original bytes','media_type':'text/plain'})
        finish_run(self.settings,run,'partial',{'updated':1,'failed':1})
        result = runs(self.settings)[0]
        self.assertEqual(result['state'],'partial')
        self.assertEqual([a['http_status'] for a in result['attempts']],[503,200])
        with connection_scope(self.settings) as connection:
            body = connection.execute('SELECT body FROM raw_asset WHERE asset_id=?',(result['attempts'][1]['raw_asset_id'],)).fetchone()[0]
        self.assertEqual(body,b'original bytes')

    def test_concurrent_migrations_recheck_after_lock(self):
        with ThreadPoolExecutor(max_workers=3) as executor:
            values = list(executor.map(lambda _:migrate(self.settings), range(6)))
        self.assertEqual(values,[13]*6)
