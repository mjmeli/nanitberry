"""End-to-end orchestration using synthetic API responses."""
import asyncio
import json
import os
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from tests.support import sync


class ServiceTests(unittest.TestCase):
    def test_profile_hours_include_late_night_continuation_but_not_next_nap(self):
        tz = ZoneInfo("America/New_York")
        evening = datetime(2020, 9, 27, 20, tzinfo=tz)
        late_end = datetime(2020, 9, 28, 8, 45, tzinfo=tz)
        nap_start = datetime(2020, 9, 28, 10, tzinfo=tz)
        entries = [
            {"type": "auto_sleep", "begin_ts": evening.timestamp(),
             "end_ts": late_end.timestamp()},
            {"type": "auto_sleep", "begin_ts": nap_start.timestamp(),
             "end_ts": (nap_start + timedelta(minutes=30)).timestamp()},
        ]

        class API:
            async def get_child(self, uid):
                return types.SimpleNamespace(nightStart=8.0, morningCutoff=7.25)

        fetched = []
        async def calendar(_, __, start, end):
            fetched.append((start, end))
            return entries
        async def history(*_): return []
        env = {"TZ": "America/New_York", "USE_HUCKLEBERRY_HOURS": "true",
               "NIGHT_START": "ignored", "MORNING_CUTOFF": "ignored",
               "MAX_WAKE_MINUTES": "20", "SYNC_DAYTIME": "false", "WRITE_ENABLED": "false"}
        with patch.dict(os.environ, env), \
             patch.object(sync, "calendar_sleep", calendar), \
             patch.object(sync, "strict_sleep_intervals", history), \
             self.assertLogs(sync.LOG, level="INFO") as captured:
            self.assertTrue(asyncio.run(sync._sync_day_with_clients(
                date(2020, 9, 27), object(), API(), [("baby", "child")])))
        self.assertEqual(fetched[0][0], datetime(2020, 9, 27, 6, 55, tzinfo=tz))
        self.assertEqual(fetched[0][1], datetime(2020, 9, 28, 20, 20, tzinfo=tz))
        logs = "\n".join(captured.output)
        self.assertIn("DRY RUN 2020-09-27 20:00:00-04:00–2020-09-28 08:45:00-04:00", logs)
        self.assertIn("2020-09-27 night: 1 Nanit interval(s)", logs)
        self.assertNotIn("10:00:00", logs)

    def test_sleep_crossing_evening_keeps_its_start_and_is_only_night(self):
        tz = ZoneInfo("America/New_York")
        start = datetime(2026, 10, 1, 19, 45, tzinfo=tz)
        end = datetime(2026, 10, 2, 7, 30, tzinfo=tz)
        entry = {"type": "auto_sleep", "begin_ts": start.timestamp(),
                 "end_ts": end.timestamp()}

        class API:
            writes = []
            async def get_child(self, uid):
                return types.SimpleNamespace(nightStart=8.0, morningCutoff=7.25)
            async def log_sleep(self, *args, **kwargs):
                self.writes.append((args, kwargs))

        async def calendar(_, __, fetch_start, fetch_end):
            self.assertLessEqual(fetch_start.timestamp(), start.timestamp())
            return [entry]
        async def history(*_): return []
        for include_day in ("false", "true"):
            with self.subTest(include_day=include_day):
                env = {"TZ": "America/New_York", "USE_HUCKLEBERRY_HOURS": "true",
                       "MAX_WAKE_MINUTES": "20", "SYNC_DAYTIME": include_day,
                       "WRITE_ENABLED": "true"}
                api = API()
                api.writes = []
                with patch.dict(os.environ, env), \
                     patch.object(sync, "calendar_sleep", calendar), \
                     patch.object(sync, "strict_sleep_intervals", history):
                    asyncio.run(sync._sync_day_with_clients(
                        date(2026, 10, 1), object(), api, [("baby", "child")],
                        now=datetime(2026, 10, 2, 8, tzinfo=tz)))
                self.assertEqual(len(api.writes), 1)
                self.assertEqual(api.writes[0][1]["start_time"], start)
                self.assertEqual(api.writes[0][1]["end_time"], end)

    def test_future_period_waits_without_fetching(self):
        class API:
            async def get_child(self, uid):
                return types.SimpleNamespace(nightStart=8.0, morningCutoff=7.25)

        async def no_calendar(*_):
            self.fail("Calendar should not be read before the requested period begins")

        with patch.dict(os.environ, {"USE_HUCKLEBERRY_HOURS": "true"}), \
             patch.object(sync, "calendar_sleep", no_calendar):
            ready = asyncio.run(sync._sync_day_with_clients(
                date(2099, 1, 1), object(), API(), [("baby", "child")]))
        self.assertFalse(ready)

    def test_cutoff_imports_finished_sleep_then_late_continuation(self):
        tz = ZoneInfo("America/New_York")
        day = date(2026, 10, 1)
        evening = datetime(2026, 10, 1, 20, tzinfo=tz)
        morning = datetime(2026, 10, 2, 7, 15, tzinfo=tz)
        early = {"type": "auto_sleep", "begin_ts": evening.timestamp(),
                 "end_ts": (evening + timedelta(hours=3)).timestamp()}
        late_start = morning - timedelta(hours=1)
        in_progress = {"type": "auto_sleep", "begin_ts": late_start.timestamp()}
        finished = {**in_progress, "end_ts": (morning + timedelta(minutes=13)).timestamp()}

        class API:
            writes = []
            async def get_child(self, uid):
                return types.SimpleNamespace(nightStart=8.0, morningCutoff=7.25)
            async def log_sleep(self, *args, **kwargs):
                self.writes.append((args, kwargs))

        api = API()
        fetched = []
        entries = [early, in_progress]
        async def calendar(_, __, start, end):
            fetched.append((start, end))
            return entries
        async def history(*_):
            return [types.SimpleNamespace(
                start=kwargs["start_time"].timestamp(),
                duration=(kwargs["end_time"] - kwargs["start_time"]).total_seconds())
                for _, kwargs in api.writes]
        env = {"TZ": "America/New_York", "USE_HUCKLEBERRY_HOURS": "true",
               "MAX_WAKE_MINUTES": "20", "SYNC_DAYTIME": "false", "WRITE_ENABLED": "true"}
        with patch.dict(os.environ, env), \
             patch.object(sync, "calendar_sleep", calendar), \
             patch.object(sync, "strict_sleep_intervals", history):
            self.assertTrue(asyncio.run(sync._sync_day_with_clients(
                day, object(), api, [("baby", "child")], now=morning)))
            self.assertEqual(len(api.writes), 1)
            self.assertEqual(api.writes[0][1]["end_time"], evening + timedelta(hours=3))
            self.assertEqual(fetched[0][1], morning)

            entries = [early, finished]
            self.assertTrue(asyncio.run(sync._sync_day_with_clients(
                day, object(), api, [("baby", "child")],
                now=morning + timedelta(minutes=30))))
            self.assertEqual(len(api.writes), 1)
            self.assertTrue(asyncio.run(sync._sync_day_with_clients(
                day, object(), api, [("baby", "child")],
                now=morning + timedelta(minutes=45))))
            self.assertEqual(len(api.writes), 2)
            self.assertEqual(api.writes[-1][1]["end_time"], morning + timedelta(minutes=13))

    def test_short_wake_at_cutoff_is_held_until_merge_window_closes(self):
        tz = ZoneInfo("America/New_York")
        morning = datetime(2026, 10, 2, 7, 15, tzinfo=tz)
        start = morning - timedelta(hours=2)
        first = {"type": "auto_sleep", "begin_ts": start.timestamp(),
                 "end_ts": (morning - timedelta(minutes=10)).timestamp()}
        second = {"type": "auto_sleep",
                  "begin_ts": (morning + timedelta(minutes=5)).timestamp(),
                  "end_ts": (morning + timedelta(minutes=40)).timestamp()}
        class API:
            writes = []
            async def get_child(self, uid):
                return types.SimpleNamespace(nightStart=8.0, morningCutoff=7.25)
            async def log_sleep(self, *args, **kwargs):
                self.writes.append((args, kwargs))

        api = API()
        entries = [first]
        async def calendar(*_): return entries
        async def history(*_): return []
        env = {"TZ": "America/New_York", "USE_HUCKLEBERRY_HOURS": "true",
               "MAX_WAKE_MINUTES": "20", "SYNC_DAYTIME": "false", "WRITE_ENABLED": "true"}
        with patch.dict(os.environ, env), \
             patch.object(sync, "calendar_sleep", calendar), \
             patch.object(sync, "strict_sleep_intervals", history):
            asyncio.run(sync._sync_day_with_clients(
                date(2026, 10, 1), object(), api, [("baby", "child")], now=morning))
            self.assertEqual(api.writes, [])
            entries = [first, second]
            asyncio.run(sync._sync_day_with_clients(
                date(2026, 10, 1), object(), api, [("baby", "child")],
                now=morning + timedelta(minutes=75)))
            self.assertEqual(len(api.writes), 1)
            self.assertEqual(api.writes[0][1]["start_time"], start)
            self.assertEqual(api.writes[0][1]["end_time"], morning + timedelta(minutes=40))

    def test_backfill_authenticates_huckleberry_once(self):
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        class API:
            authentications = 0
            def __init__(self, **kwargs): pass
            async def authenticate(self):
                API.authentications += 1

        dates = []
        async def calendar(_, __, start, end):
            dates.append(start.date())
            return []
        async def history(*_): return []
        env = {"TZ": "America/New_York", "CHILD_UID_MAP": '{"baby":"child"}',
               "HUCKLEBERRY_EMAIL": "test@example.invalid",
               "HUCKLEBERRY_PASSWORD": "unused", "WRITE_ENABLED": "false",
               "SYNC_DAYTIME": "false", "USE_HUCKLEBERRY_HOURS": "false"}
        original_state, original_status = sync.STATE, sync.STATUS
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sync.STATE = Path(tmp) / "nanit_tokens.json"
                sync.STATUS = Path(tmp) / "sync_status.json"
                with patch.dict(os.environ, env), \
                     patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
                     patch.object(sync, "HuckleberryAPI", API), \
                     patch.object(sync, "restore_nanit", lambda _: object()), \
                     patch.object(sync, "validate_uid_pairs", new_callable=AsyncMock), \
                     patch.object(sync, "calendar_sleep", calendar), \
                     patch.object(sync, "strict_sleep_intervals", history):
                    asyncio.run(sync.backfill(date(2020, 9, 28), 2))
                self.assertEqual(API.authentications, 1)
                self.assertEqual(dates, [date(2020, 9, 27), date(2020, 9, 28)])
        finally:
            sync.STATE, sync.STATUS = original_state, original_status

    def test_health_reports_reauth_and_stale_runs(self):
        original_state, original_status = sync.STATE, sync.STATUS
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sync.STATE = Path(tmp) / "nanit_tokens.json"
                sync.STATUS = Path(tmp) / "sync_status.json"
                self.assertIn("nanit_reauth_required", sync.health_status()[1])
                sync.STATE.write_text("{}")
                self.assertIn("nanit_reauth_required", sync.health_status()[1])
                sync.save_tokens({"access_token": "a", "refresh_token": "r"})
                sync.status_update("waiting", "scheduler_started", service_started=sync.utc_now())
                self.assertIn("no_successful_run", sync.health_status()[1])
                sync.status_update("error", "nanit_reauth_required", "Run interactive login")
                self.assertIn("nanit_reauth_required", sync.health_status()[1])
                sync.status_update("ok", "sync_succeeded", last_success="2020-01-01T00:00:00+00:00")
                self.assertIn("sync_stale", sync.health_status()[1])
        finally:
            sync.STATE, sync.STATUS = original_state, original_status

    def test_full_sync_fails_closed_on_history_error_or_bad_calendar_data(self):
        evening = datetime(2020, 9, 27, 20, tzinfo=ZoneInfo("America/New_York"))
        valid = {"type": "auto_sleep", "begin_ts": evening.timestamp(),
                 "end_ts": (evening + timedelta(hours=2)).timestamp()}
        malformed = {"type": "auto_sleep", "begin_ts": evening.timestamp()}

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        class API:
            writes = []
            def __init__(self, **kwargs): pass
            async def authenticate(self): pass
            async def log_sleep(self, *args, **kwargs): self.writes.append((args, kwargs))

        env = {"TZ": "America/New_York", "CHILD_UID_MAP": '{"baby":"child"}',
               "HUCKLEBERRY_EMAIL": "test@example.invalid",
               "HUCKLEBERRY_PASSWORD": "unused", "WRITE_ENABLED": "true",
               "SYNC_DAYTIME": "false", "USE_HUCKLEBERRY_HOURS": "false"}
        original_state, original_status = sync.STATE, sync.STATUS
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sync.STATE = Path(tmp) / "nanit_tokens.json"
                sync.STATUS = Path(tmp) / "sync_status.json"
                sync.save_tokens({"access_token": "a", "refresh_token": "r"})
                for scenario in ("history_unavailable", "malformed_calendar"):
                    with self.subTest(scenario=scenario):
                        async def calendar(*_):
                            return [malformed if scenario == "malformed_calendar" else valid]

                        async def history(*_):
                            if scenario == "history_unavailable":
                                raise ConnectionError("Firestore unavailable")
                            return []

                        with patch.dict(os.environ, env), \
                             patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
                             patch.object(sync, "HuckleberryAPI", API), \
                             patch.object(sync, "restore_nanit", lambda _: object()), \
                             patch.object(sync, "validate_uid_pairs", new_callable=AsyncMock), \
                             patch.object(sync, "calendar_sleep", calendar), \
                             patch.object(sync, "strict_sleep_intervals", history):
                            with self.assertRaises((ConnectionError, ValueError)):
                                asyncio.run(sync.sync_day(date(2020, 9, 27)))
                        self.assertEqual(API.writes, [])
                        self.assertEqual(json.loads(sync.STATUS.read_text())["state"], "error")
                        self.assertFalse(sync.health_status()[0])
        finally:
            sync.STATE, sync.STATUS = original_state, original_status

    def test_dry_run_with_available_history_reports_success_without_write(self):
        evening = datetime(2020, 9, 27, 20, tzinfo=ZoneInfo("America/New_York"))
        entry = {"type": "auto_sleep", "begin_ts": evening.timestamp(),
                 "end_ts": (evening + timedelta(hours=2)).timestamp()}

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        class API:
            writes = []
            def __init__(self, **kwargs): pass
            async def authenticate(self): pass
            async def log_sleep(self, *args, **kwargs): self.writes.append((args, kwargs))

        async def calendar(*_): return [entry]
        async def history(*_): return []
        env = {"TZ": "America/New_York", "CHILD_UID_MAP": '{"baby":"child"}',
               "HUCKLEBERRY_EMAIL": "test@example.invalid",
               "HUCKLEBERRY_PASSWORD": "unused", "WRITE_ENABLED": "false",
               "SYNC_DAYTIME": "false", "USE_HUCKLEBERRY_HOURS": "false"}
        original_state, original_status = sync.STATE, sync.STATUS
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sync.STATE = Path(tmp) / "nanit_tokens.json"
                sync.STATUS = Path(tmp) / "sync_status.json"
                sync.save_tokens({"access_token": "a", "refresh_token": "r"})
                with patch.dict(os.environ, env), \
                     patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
                     patch.object(sync, "HuckleberryAPI", API), \
                     patch.object(sync, "restore_nanit", lambda _: object()), \
                     patch.object(sync, "validate_uid_pairs", new_callable=AsyncMock), \
                     patch.object(sync, "calendar_sleep", calendar), \
                     patch.object(sync, "strict_sleep_intervals", history):
                    asyncio.run(sync.sync_day(date(2020, 9, 27)))
                self.assertEqual(API.writes, [])
                self.assertEqual(json.loads(sync.STATUS.read_text())["state"], "ok")
                self.assertTrue(sync.health_status()[0])
        finally:
            sync.STATE, sync.STATUS = original_state, original_status
