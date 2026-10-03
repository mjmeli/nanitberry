"""Persistence, live-source revisions, and atomic manual-edit protection."""
import asyncio
import copy
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from tests.support import sync


class PreconditionFailed(Exception): pass
class AlreadyExists(Exception): pass
class NotFound(Exception): pass


class Version:
    def __init__(self, value): self.value = value
    def rfc3339(self): return f"2026-10-03T00:00:00.{self.value:09d}Z"


class Ref:
    def __init__(self, client, path): self.client, self.path = client, path
    def collection(self, name): return Ref(self.client, self.path + '/' + name)
    def document(self, name): return Ref(self.client, self.path + '/' + name)
    async def get(self):
        data, version = self.client.rows.get(self.path, (None, None))
        return types.SimpleNamespace(exists=data is not None, update_time=version,
                                     to_dict=lambda: copy.deepcopy(data))


class Database:
    def __init__(self):
        self.rows = {}
        self.counter = 0
        self.before_commit = None
        self.fail_after_commit = False
        self.commits = 0
    def collection(self, name): return Ref(self, name)
    def put(self, path, data):
        self.counter += 1
        self.rows[path] = (copy.deepcopy(data), Version(self.counter))
    def batch(self):
        db = self
        class Batch:
            def __init__(self): self.ops = []
            def create(self, ref, data): self.ops.append((ref.path, data, None, True))
            def update(self, ref, data, option): self.ops.append((ref.path, data, option, False))
            async def commit(self):
                if db.before_commit:
                    callback, db.before_commit = db.before_commit, None
                    callback()
                for path, data, expected, create in self.ops:
                    present = db.rows.get(path)
                    if create and present: raise AlreadyExists()
                    if not create and not present: raise NotFound()
                    if not create and present[1].rfc3339() != expected.rfc3339():
                        raise PreconditionFailed()
                results = []
                for path, data, expected, create in self.ops:
                    merged = {} if create else copy.deepcopy(db.rows[path][0])
                    for key, value in data.items():
                        cursor = merged
                        fields = key.split('.')
                        for field in fields[:-1]: cursor = cursor.setdefault(field, {})
                        cursor[fields[-1]] = copy.deepcopy(value)
                    db.put(path, merged)
                    results.append(types.SimpleNamespace(update_time=db.rows[path][1]))
                db.commits += 1
                if db.fail_after_commit:
                    db.fail_after_commit = False
                    raise TimeoutError('response lost after commit')
                return results
        return Batch()


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_patch = patch.object(sync, 'STATE', Path(self.tmp.name) / 'nanit_tokens.json')
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.env_patch = patch.dict(os.environ, {'OWNERSHIP_RETENTION_DAYS': '90'})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        errors = types.ModuleType('google.api_core.exceptions')
        errors.AlreadyExists, errors.FailedPrecondition, errors.NotFound = AlreadyExists, PreconditionFailed, NotFound
        firestore = types.ModuleType('google.cloud.firestore_v1')
        firestore.LastUpdateOption = lambda version: version
        self.modules = patch.dict(sys.modules, {'google.api_core.exceptions': errors,
                                               'google.cloud.firestore_v1': firestore})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.now = datetime(2026, 10, 3, 8, tzinfo=timezone.utc)
        self.start = self.now - timedelta(hours=6)
        self.span = (self.start, self.start + timedelta(minutes=6))
        self.db = Database()
        self.root = 'sleep/child'
        self.db.put(self.root, {'prefs': {'sweetSpotWhich': 4}, 'timer': {'active': True}})
        async def client(): return self.db
        async def offset(): return -240
        self.api = types.SimpleNamespace(_get_firestore_client=client, _get_timezone_offset_minutes=offset)
        async def history(api, child, start, end):
            entries = []
            for path, (data, _) in self.db.rows.items():
                if path.startswith(self.root + '/intervals/'):
                    entry = types.SimpleNamespace(start=data['start'], duration=data['duration'],
                                                  document_id=path.split('/')[-1])
                    if sync.overlap(sync.existing_range(entry), (start, end)): entries.append(entry)
            return entries
        self.history_patch = patch.object(sync, 'strict_sleep_intervals', history)
        self.history_patch.start()
        self.addCleanup(self.history_patch.stop)
        self.store = sync.SleepOwnership(self.now.timestamp())

    def apply(self, span=None, write=True):
        asyncio.run(self.store.sync_span(self.api, 'baby', 'child', span or self.span,
                                        write=write, kind='night'))
    def created(self):
        self.apply()
        key = next(iter(self.store.data['records']))
        return key, self.root + '/intervals/' + key
    def reload(self, days=0):
        self.store = sync.SleepOwnership((self.now + timedelta(days=days)).timestamp())

    def poll(self, entries, now, *, day=None, gap='20', daytime='false'):
        async def calendar(_, __, start, end):
            return [entry for entry in entries if entry['begin_ts'] < end.timestamp()
                    and (entry.get('end_ts') is None or entry['end_ts'] > start.timestamp())]
        env = {'TZ': 'UTC', 'USE_HUCKLEBERRY_HOURS': 'false', 'NIGHT_START': '20:00',
               'MORNING_CUTOFF': '07:00', 'WRITE_ENABLED': 'true',
               'MAX_WAKE_MINUTES': gap, 'SYNC_DAYTIME': daytime}
        with patch.dict(os.environ, env), patch.object(sync, 'calendar_sleep', calendar):
            asyncio.run(sync._sync_day_with_clients(day or (now - timedelta(days=1)).date(),
                                                   object(), self.api, [('baby', 'child')], now=now))

    def test_immediate_partial_import_then_short_wake_continuation_updates_one_row(self):
        entries = [{'type': 'auto_sleep', 'begin_ts': self.start.timestamp(),
                    'end_ts': self.span[1].timestamp()},
                   {'type': 'auto_sleep', 'begin_ts': (self.start + timedelta(minutes=10)).timestamp()}]
        self.poll(entries, self.start + timedelta(minutes=12))
        self.reload()
        key = next(iter(self.store.data['records']))
        path = self.root + '/intervals/' + key
        self.assertEqual(self.db.rows[path][0]['duration'], 360)
        entries[1]['end_ts'] = (self.start + timedelta(minutes=20)).timestamp()
        self.poll(entries, self.start + timedelta(minutes=20))
        self.reload()
        self.assertEqual(len(self.store.data['records']), 1)
        self.assertEqual(self.db.rows[path][0]['duration'], 1200)

    def test_long_wake_and_zero_gap_keep_immediate_imports_separate(self):
        for gap, resume in [('20', 27), ('0', 10)]:
            with self.subTest(gap=gap):
                self.db.rows = {self.root: ({'prefs': {}}, Version(1))}
                self.store.path.unlink(missing_ok=True)
                entries = [{'type': 'auto_sleep', 'begin_ts': self.start.timestamp(),
                            'end_ts': self.span[1].timestamp()},
                           {'type': 'auto_sleep', 'begin_ts': (self.start + timedelta(minutes=resume)).timestamp(),
                            'end_ts': (self.start + timedelta(minutes=resume + 10)).timestamp()}]
                self.poll(entries, self.start + timedelta(minutes=resume + 10), gap=gap)
                self.reload()
                self.assertEqual(len(self.store.data['records']), 2)

    def test_open_and_future_ended_records_do_not_get_invented_end_times(self):
        entries = [{'type': 'auto_sleep', 'begin_ts': self.start.timestamp()},
                   {'type': 'auto_sleep', 'begin_ts': (self.start + timedelta(minutes=10)).timestamp(),
                    'end_ts': (self.start + timedelta(minutes=20)).timestamp()}]
        self.poll(entries, self.start + timedelta(minutes=15))
        self.assertEqual(self.db.commits, 0)

    def test_immediate_import_across_morning_cutoff_stays_one_night_entry(self):
        start = self.now.replace(hour=6, minute=30)
        first_end = self.now.replace(hour=6, minute=55)
        entries = [{'type': 'auto_sleep', 'begin_ts': start.timestamp(), 'end_ts': first_end.timestamp()}]
        self.poll(entries, first_end, daytime='true')
        entries.append({'type': 'auto_sleep', 'begin_ts': self.now.replace(hour=7, minute=5).timestamp(),
                        'end_ts': self.now.replace(hour=7, minute=40).timestamp()})
        self.poll(entries, self.now.replace(hour=7, minute=40), day=self.now.date(), daytime='true')
        self.assertEqual(self.db.commits, 1)  # Today's query must not create a nap.
        self.poll(entries, self.now.replace(hour=7, minute=40), daytime='true')
        self.reload()
        self.assertEqual(len(self.store.data['records']), 1)
        key = next(iter(self.store.data['records']))
        self.assertEqual(self.db.rows[self.root + '/intervals/' + key][0]['duration'], 70 * 60)

    def test_live_revision_after_restart_updates_same_row_and_last_sleep(self):
        key, path = self.created()
        self.reload()
        # Reproduce the real six-minute provisional interval extending past an hour.
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.assertEqual(self.db.rows[path][0]['duration'], 78 * 60)
        self.assertEqual(len(self.store.data['records']), 1)
        self.assertEqual(self.db.rows[self.root][0]['prefs']['lastSleep']['duration'], 78 * 60)
        self.assertEqual(self.db.rows[self.root][0]['prefs']['sweetSpotWhich'], 4)
        self.assertTrue(self.db.rows[self.root][0]['timer']['active'])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        commits = self.db.commits
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.assertEqual(self.db.commits, commits)

    def test_manual_edit_even_same_values_with_new_version_releases_ownership(self):
        key, path = self.created()
        self.db.put(path, self.db.rows[path][0])
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.assertEqual(self.store.data['records'][key]['state'], 'released')
        self.assertEqual(self.db.rows[path][0]['duration'], 360)
        self.reload()
        self.apply()
        self.assertEqual(self.db.commits, 1)

    def test_manual_notes_and_deletion_are_preserved(self):
        key, path = self.created()
        data = copy.deepcopy(self.db.rows[path][0])
        data['details'] = {'notes': 'Manual correction'}
        self.db.put(path, data)
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.assertEqual(self.db.rows[path][0], data)
        del self.db.rows[path]
        self.reload()
        self.apply()
        self.assertNotIn(path, self.db.rows)
        self.assertEqual(self.store.data['records'][key]['state'], 'released')

    def test_deleted_owned_row_is_not_recreated(self):
        key, path = self.created()
        del self.db.rows[path]
        self.apply()
        self.assertNotIn(path, self.db.rows)
        self.assertEqual(self.store.data['records'][key]['state'], 'released')

    def test_edit_between_read_and_write_rejects_atomic_batch(self):
        key, path = self.created()
        before_parent = copy.deepcopy(self.db.rows[self.root][0])
        edited = {**self.db.rows[path][0], 'details': {'notes': 'Concurrent edit'}}
        self.db.before_commit = lambda: self.db.put(path, edited)
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.assertEqual(self.db.rows[path][0], edited)
        self.assertEqual(self.db.rows[self.root][0], before_parent)
        self.assertEqual(self.store.data['records'][key]['state'], 'released')

    def test_parent_race_retries_without_partial_interval_update(self):
        key, path = self.created()
        self.db.before_commit = lambda: self.db.put(self.root, self.db.rows[self.root][0])
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.assertEqual(self.db.rows[path][0]['duration'], 360)
        self.assertEqual(self.store.data['records'][key]['state'], 'owned')
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.assertEqual(self.db.rows[path][0]['duration'], 78 * 60)

    def test_extension_cannot_overlap_manual_sleep(self):
        key, path = self.created()
        manual = {'start': int((self.start + timedelta(minutes=30)).timestamp()), 'duration': 600}
        self.db.put(self.root + '/intervals/manual', manual)
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.assertEqual(self.db.rows[path][0]['duration'], 360)
        self.assertEqual(self.db.rows[self.root + '/intervals/manual'][0], manual)

    def test_dry_run_has_no_remote_or_local_writes(self):
        self.apply(write=False)
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.db.commits, 0)
        key, path = self.created()
        before = self.store.path.read_bytes()
        self.apply((self.start, self.start + timedelta(minutes=78)), write=False)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.db.rows[path][0]['duration'], 360)

    def test_timeout_after_create_is_journaled_and_never_duplicated(self):
        self.db.fail_after_commit = True
        with self.assertRaises(TimeoutError): self.apply()
        self.reload()
        self.apply()
        self.assertEqual(self.db.commits, 1)
        self.assertEqual(next(iter(self.store.data['records'].values()))['state'], 'released')

    def test_timeout_after_update_does_not_adopt_unknown_version(self):
        key, path = self.created()
        self.db.fail_after_commit = True
        with self.assertRaises(TimeoutError):
            self.apply((self.start, self.start + timedelta(minutes=78)))
        self.reload()
        self.apply((self.start, self.start + timedelta(minutes=90)))
        self.assertEqual(self.db.rows[path][0]['duration'], 78 * 60)
        self.assertEqual(self.store.data['records'][key]['state'], 'released')

    def test_retention_expires_from_creation_without_deleting_remote_history(self):
        key, path = self.created()
        self.reload(days=89)
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.reload(days=91)
        self.store.save()
        self.assertEqual(self.store.data['records'], {})
        self.assertIn(path, self.db.rows)
        self.apply((self.start, self.start + timedelta(minutes=90)))
        self.assertEqual(self.db.rows[path][0]['duration'], 78 * 60)

    def test_corrupt_journal_fails_closed(self):
        self.store.path.write_text('{broken')
        with self.assertRaises(ValueError): self.reload()
        self.store.path.write_text(json.dumps({'version': 9, 'records': {}}))
        with self.assertRaises(ValueError): self.reload()

    def test_merging_multiple_owned_rows_requires_review(self):
        self.created()
        second = (self.start + timedelta(minutes=30), self.start + timedelta(minutes=40))
        self.apply(second)
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.assertEqual(self.db.commits, 2)
        self.assertEqual(len(self.store.data['records']), 2)

    def test_newer_last_sleep_is_not_replaced_by_older_revision(self):
        key, path = self.created()
        newer = {'start': int(self.now.timestamp()), 'duration': 100, 'offset': -240}
        self.db.put(self.root, {'prefs': {'lastSleep': newer}})
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.assertEqual(self.db.rows[self.root][0]['prefs']['lastSleep'], newer)
        self.assertEqual(self.db.rows[path][0]['duration'], 78 * 60)

    def test_manually_adjusted_last_sleep_preference_is_preserved(self):
        key, path = self.created()
        manual = {'start': int(self.start.timestamp()), 'duration': 500, 'offset': -240}
        self.db.put(self.root, {'prefs': {'lastSleep': manual}})
        self.apply((self.start, self.start + timedelta(minutes=78)))
        self.assertEqual(self.db.rows[self.root][0]['prefs']['lastSleep'], manual)

    def test_real_orchestration_reconciles_later_calendar_revision(self):
        entries = [{'type': 'auto_sleep', 'begin_ts': self.start.timestamp(),
                    'end_ts': self.span[1].timestamp()}]
        async def calendar(*_): return entries
        env = {'TZ': 'UTC', 'USE_HUCKLEBERRY_HOURS': 'false', 'NIGHT_START': '20:00',
               'MORNING_CUTOFF': '07:00', 'WRITE_ENABLED': 'true',
               'MAX_WAKE_MINUTES': '20', 'SYNC_DAYTIME': 'false'}
        with patch.dict(os.environ, env), patch.object(sync, 'calendar_sleep', calendar):
            day = (self.now - timedelta(days=1)).date()
            asyncio.run(sync._sync_day_with_clients(day, object(), self.api,
                                                   [('baby', 'child')], now=self.now))
            entries[0]['end_ts'] = (self.start + timedelta(minutes=78)).timestamp()
            asyncio.run(sync._sync_day_with_clients(day, object(), self.api,
                                                   [('baby', 'child')], now=self.now))
        self.reload()
        key = next(iter(self.store.data['records']))
        self.assertEqual(self.db.rows[self.root + '/intervals/' + key][0]['duration'], 78 * 60)
        self.assertEqual(self.db.commits, 2)

    def test_orchestration_does_not_adopt_preexisting_imports(self):
        entries = [{'type': 'auto_sleep', 'begin_ts': self.start.timestamp(),
                    'end_ts': (self.start + timedelta(minutes=78)).timestamp()}]
        async def calendar(*_): return entries
        self.db.put(self.root + '/intervals/legacy',
                    {'start': int(self.start.timestamp()), 'duration': 360})
        env = {'TZ': 'UTC', 'USE_HUCKLEBERRY_HOURS': 'false', 'NIGHT_START': '20:00',
               'MORNING_CUTOFF': '07:00', 'WRITE_ENABLED': 'true',
               'MAX_WAKE_MINUTES': '20', 'SYNC_DAYTIME': 'false'}
        with patch.dict(os.environ, env), patch.object(sync, 'calendar_sleep', calendar):
            asyncio.run(sync._sync_day_with_clients((self.now - timedelta(days=1)).date(),
                                                   object(), self.api, [('baby', 'child')], now=self.now))
        self.reload()
        self.assertEqual(self.store.data['records'], {})
        self.assertEqual(self.db.commits, 0)

    def test_retention_bounds_and_protected_deletion_expire(self):
        for days in ('0', '6', '3651'):
            with patch.dict(os.environ, {'OWNERSHIP_RETENTION_DAYS': days}):
                with self.assertRaises(ValueError): self.reload()
        key, path = self.created()
        del self.db.rows[path]
        self.apply()
        self.reload(days=89)
        self.assertIn(key, self.store.data['records'])
        self.reload(days=91)
        self.assertNotIn(key, self.store.data['records'])
