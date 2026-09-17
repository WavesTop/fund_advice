import copy
from datetime import date, datetime
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage.database import connection_scope, migrate
from backend.storage.research import append_facts, freeze_snapshot, get_snapshot, quarantine, canonical, normalize_fact
from research_fixtures import NOW, SUBJECT, numeric, series, clock


class ResearchStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / 'research.sqlite3')

    def append(self, facts=None):
        facts = facts or [numeric()]
        return append_facts(self.settings, facts, source_url='https://example.test/original', raw_body=canonical(facts).encode())

    def test_idempotent_observation_does_not_change_first_seen_or_revision(self):
        with clock(NOW):
            original = self.append()
        with clock('2026-09-16T13:01:00.000000Z'):
            repeated = self.append()
            snap = freeze_snapshot(self.settings, [SUBJECT])
        self.assertEqual(original['revision_ids'], repeated['revision_ids'])
        self.assertEqual(repeated['new_revision_count'], 0)
        fact = snap['manifest']['facts'][0]
        self.assertEqual(fact['first_seen_at'], NOW)
        self.assertEqual(fact['last_observed_at'], '2026-09-16T13:01:00.000000Z')
        with connection_scope(self.settings) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM data_observation').fetchone()[0], 2)

    def test_a_b_a_is_three_versions_and_old_snapshot_stays_a(self):
        versions = []
        for minute, value in enumerate(('10', '20', '10')):
            with clock(f'2026-09-16T13:0{minute}:00.000000Z'):
                versions += self.append([numeric(value=value)])['revision_ids']
                if not minute:
                    old = freeze_snapshot(self.settings, [SUBJECT])
        self.assertEqual(len(set(versions)), 3)
        self.assertEqual(get_snapshot(self.settings, old['snapshot_id'])['manifest']['facts'][0]['value'], '10')
        with clock('2026-09-16T13:03:00.000000Z'):
            middle = freeze_snapshot(self.settings, [SUBJECT], cutoff='2026-09-16T13:01:30Z')
            current = freeze_snapshot(self.settings, [SUBJECT])
        self.assertEqual(middle['manifest']['facts'][0]['value'], '20')
        self.assertEqual(current['manifest']['facts'][0]['supersedes_revision_id'], versions[1])

    def test_today_imported_old_report_is_absent_from_earlier_cutoff(self):
        with clock(NOW):
            self.append([numeric(published_at='2026-08-27T10:00:00+08:00', publication_precision='time')])
            before = freeze_snapshot(self.settings, [SUBJECT], cutoff='2026-09-15T13:00:00Z')
        self.assertEqual(before['manifest']['facts'], [])
        self.assertEqual(before['manifest']['missing_subjects'], [SUBJECT])

    def test_day_precision_is_next_source_midnight_not_fake_235959(self):
        with clock(NOW):
            self.append([numeric(published_at='2026-09-16', publication_precision='day')])
            same_day = freeze_snapshot(self.settings, [SUBJECT])
        with clock('2026-09-16T16:00:00.000000Z'):
            midnight = freeze_snapshot(self.settings, [SUBJECT])
        self.assertEqual(same_day['manifest']['facts'], [])
        self.assertEqual(midnight['manifest']['facts'][0]['published_at'], '2026-09-16T16:00:00.000000Z')

    def test_user_cannot_supply_known_times_or_unverified_public_mode(self):
        with clock(NOW):
            for field in ('first_seen_at', 'committed_at', 'revision_id', 'available_at'):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    self.append([numeric(**{field: NOW})])
            with self.assertRaises(ValueError):
                freeze_snapshot(self.settings, [SUBJECT], mode='public_as_of')
            with self.assertRaises(ValueError):
                freeze_snapshot(self.settings, [SUBJECT], cutoff='2026-09-17T13:00:00Z')

    def test_future_publication_and_invalid_timezone_fail(self):
        with clock(NOW):
            for fields in ({'published_at':'2026-09-17T00:00:00Z','publication_precision':'time'},
                           {'published_at':'2026-09-16','publication_precision':'day','source_timezone':'Bad/Timezone'},
                           {'published_at':'2026-09-16','publication_precision':'day','source_timezone':42}):
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    self.append([numeric(**fields)])

    def test_invalid_numbers_or_duplicate_batch_are_atomic(self):
        with clock(NOW):
            good = self.append()
            for value in ('NaN', 'Infinity', '-Infinity', '1e40', '1e-50', 1, True):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.append([numeric(value=value)])
            with self.assertRaises(ValueError):
                self.append([numeric(), numeric(value='12')])
            snap = freeze_snapshot(self.settings, [SUBJECT])
        self.assertEqual([f['revision_id'] for f in snap['manifest']['facts']], good['revision_ids'])

    def test_bad_series_cannot_become_total_return_or_cross_calendar(self):
        fact = series(count=1)[0]
        with clock(NOW):
            for changes in ({'effective_date':'2026-10-01'}, {'effective_date':'2026-09-13'},
                            {'series_kind':'total_return', 'basis':'unit_nav'}, {'currency':'USD'}, {'value':'0'}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    self.append([{**fact, **changes}])

    def test_quarantined_correction_does_not_resurrect_old_or_autoheal(self):
        with clock(NOW):
            self.append()
        with clock('2026-09-16T13:01:00.000000Z'):
            correction = self.append([numeric(value='20')])
        with clock('2026-09-16T13:02:00.000000Z'):
            quarantine(self.settings, correction['revision_ids'][0], '错误口径')
            self.append([numeric(value='20')])
            snap = freeze_snapshot(self.settings, [SUBJECT])
        self.assertEqual(snap['manifest']['facts'], [])
        self.assertEqual(snap['manifest']['excluded'][0]['kind'], 'quality')
        self.assertEqual(snap['manifest']['excluded'][0]['subject_key'], SUBJECT)

    def test_database_rejects_updating_or_deleting_audit_objects(self):
        with clock(NOW):
            self.append()
            freeze_snapshot(self.settings, [SUBJECT])
        with connection_scope(self.settings) as connection:
            for table in ('raw_asset','data_revision','numeric_fact','data_observation','data_quality_event','data_snapshot','snapshot_item'):
                with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(f'DELETE FROM {table}')
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE numeric_fact SET value='999'")

    def test_source_conflicts_stay_separate_and_observation_does_not_leak(self):
        with clock(NOW):
            self.append()
            self.append([numeric(source_id='fixture.other')])
            snap = freeze_snapshot(self.settings, [SUBJECT])
        self.assertEqual(len(snap['manifest']['facts']), 2)
        self.assertEqual(len(snap['manifest']['source_conflicts']), 1)
        with clock('2026-09-16T13:02:00.000000Z'):
            self.append()
            old = freeze_snapshot(self.settings, [SUBJECT], cutoff=NOW)
        self.assertTrue(all(f['last_observed_at'] == NOW for f in old['manifest']['facts']))

    def test_clock_rollback_after_identical_observation_is_blocked(self):
        with clock(NOW): self.append()
        with clock('2026-09-16T13:02:00.000000Z'): self.append()
        with clock('2026-09-16T13:01:00.000000Z'), self.assertRaises(ValueError): self.append()

    def test_fact_not_published_until_post_commit_quality_event(self):
        with patch('backend.storage.research.utc_now', side_effect=[NOW, '2026-09-16T13:00:01.000000Z']):
            self.append()
        with clock('2026-09-16T13:00:02.000000Z'):
            between = freeze_snapshot(self.settings, [SUBJECT], cutoff='2026-09-16T13:00:00.500000Z')
            after = freeze_snapshot(self.settings, [SUBJECT])
        self.assertEqual(between['manifest']['facts'], [])
        self.assertEqual(len(after['manifest']['facts']), 1)

    def test_empty_nonexistent_snapshot_explicit_not_found(self):
        with self.assertRaises(AppError) as error:
            get_snapshot(self.settings, 'absent')
        self.assertEqual(error.exception.status_code, 404)

    def test_scope_and_subject_identity_not_name_matching(self):
        with clock(NOW):
            self.append([numeric(subject='tracked_index:other:A')])
            snap = freeze_snapshot(self.settings, [SUBJECT])
        self.assertEqual(snap['manifest']['facts'], [])
        with self.assertRaises(ValueError):
            normalize_fact(numeric(subject='A'), NOW)

    def test_decimal_ingestion_preserves_more_than_context_precision(self):
        value = '12345678901234567890123456789.123456789012345678901234567890'
        with clock(NOW):
            self.append([numeric(value=value)])
            snapshot = freeze_snapshot(self.settings, [SUBJECT])
        self.assertEqual(snapshot['manifest']['facts'][0]['value'], value.rstrip('0'))

    def test_backup_restores_raw_bodies_and_original_snapshot_without_wal_copy(self):
        from backend.storage.research_backup import backup_database
        with clock(NOW):
            self.append()
            snapshot = freeze_snapshot(self.settings, [SUBJECT])
        target = Path(self.temp.name)/'restored.sqlite3'
        result = backup_database(self.settings, target)
        self.assertEqual(result['integrity'], 'ok')
        restored = Settings(database_path=target)
        self.assertEqual(get_snapshot(restored, snapshot['snapshot_id'])['manifest'], snapshot['manifest'])
        with self.assertRaises(ValueError): backup_database(self.settings, target)
        with self.assertRaises(ValueError): backup_database(self.settings, self.settings.database_path)
