"""Check the sleep policy without contacting either account."""
import importlib.util
import asyncio
import json
import os
import tempfile
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

fake_aiohttp = types.ModuleType("aiohttp")
fake_aiohttp.ClientError = type("ClientError", (Exception,), {})
fake_aiohttp.ClientResponseError = type("ClientResponseError", (fake_aiohttp.ClientError,), {})
fake_aiohttp.ClientTimeout = lambda **kwargs: kwargs
sys.modules.setdefault("aiohttp", fake_aiohttp)
fake_nanit = types.ModuleType("aionanit")
fake_nanit.NanitAuthError = type("NanitAuthError", (Exception,), {})
fake_nanit.NanitConnectionError = type("NanitConnectionError", (Exception,), {})
fake_nanit.NanitMfaRequiredError = type("NanitMfaRequiredError", (fake_nanit.NanitAuthError,), {})
fake_nanit.NanitClient = object
sys.modules.setdefault("aionanit", fake_nanit)
fake_api = types.ModuleType("huckleberry_api")
fake_api.HuckleberryAPI = object
sys.modules.setdefault("huckleberry_api", fake_api)
spec = importlib.util.spec_from_file_location("sync", __file__.replace("tests/test_intervals.py", "sync.py"))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class IntervalTests(unittest.TestCase):
    def test_short_feed_bridged_long_wake_excluded(self):
        tz = ZoneInfo("America/New_York")
        start = datetime(2026, 9, 27, 20, tzinfo=tz)
        end = datetime(2026, 9, 28, 7, tzinfo=tz)
        segments = [(0, 3 * 3600), (3 * 3600 + 600, 7 * 3600),
                    (7 * 3600 + 2700, 11 * 3600)]
        entries = [{"type": "auto_sleep", "begin_ts": int(start.timestamp()) + a,
                    "end_ts": int(start.timestamp()) + b} for a, b in segments]
        spans = sync.normalize(entries, start, end, 20 * 60)
        self.assertEqual(len(spans), 2)
        self.assertEqual(int((spans[1][0] - spans[0][1]).total_seconds()), 2700)

    def test_manual_nap_is_excluded_and_bad_auto_sleep_fails_closed(self):
        tz = ZoneInfo("America/New_York")
        start = datetime(2026, 9, 27, 10, tzinfo=tz)
        end = datetime(2026, 9, 27, 18, tzinfo=tz)
        manual = {"type": "sleep", "time": start.timestamp(), "duration": 1800}
        self.assertEqual(sync.normalize([manual], start, end, 0), [])
        with self.assertRaises(ValueError):
            sync.normalize([{"type": "auto_sleep", "begin_ts": 123}], start, end, 0)

    def test_fall_dst_overlap_uses_instants(self):
        tz = ZoneInfo("America/New_York")
        first = (datetime(2026, 11, 1, 1, 50, tzinfo=tz, fold=0),
                 datetime(2026, 11, 1, 1, 10, tzinfo=tz, fold=1))
        second = (datetime(2026, 11, 1, 1, 5, tzinfo=tz, fold=1),
                  datetime(2026, 11, 1, 1, 20, tzinfo=tz, fold=1))
        self.assertTrue(sync.overlap(first, second))
        night = sync.windows(date(2026, 10, 31), tz,
                             sync.parse_clock("18:00"), sync.parse_clock("10:00"), False)[0]
        self.assertEqual(night[2].timestamp() - night[1].timestamp(), 17 * 3600)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            sync.local_boundary(date(2026, 11, 1), sync.parse_clock("01:30"), tz)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            sync.local_boundary(date(2026, 3, 8), sync.parse_clock("02:30"), tz)

    def test_existing_fractional_interval_is_not_truncated(self):
        old = sync.existing_range(types.SimpleNamespace(start=100.8, duration=0.5))
        new = (datetime.fromtimestamp(101.0, ZoneInfo("UTC")),
               datetime.fromtimestamp(102.0, ZoneInfo("UTC")))
        self.assertTrue(sync.overlap(new, old))

    def test_day_starts_at_morning_cutoff_and_overlaps_skip(self):
        tz = ZoneInfo("America/New_York")
        day, night = sync.windows(date(2026, 9, 27), tz,
                                  sync.parse_clock("18:00"), sync.parse_clock("10:00"), True)
        self.assertEqual(day[1].hour, 10)
        self.assertEqual(day[2], night[1])
        self.assertTrue(sync.overlap((night[1], night[1] + timedelta(minutes=10)),
                                     (night[1] + timedelta(minutes=9), night[1] + timedelta(minutes=20))))

    def test_backfill_includes_latest_night_and_runs_oldest_first(self):
        self.assertEqual(sync.backfill_dates(date(2026, 9, 27), 3),
                         [date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27)])
        with self.assertRaises(ValueError):
            sync.backfill_dates(date(2026, 9, 27), 0)

    def test_failed_history_read_stops_sync(self):
        google = types.ModuleType("google")
        cloud = types.ModuleType("google.cloud")
        firestore = types.ModuleType("google.cloud.firestore")
        firestore.FieldFilter = lambda *args: args
        cloud.firestore = firestore
        google.cloud = cloud
        sys.modules.update({"google": google, "google.cloud": cloud,
                            "google.cloud.firestore": firestore})
        types_module = types.ModuleType("huckleberry_api.firebase_types")
        types_module.FirebaseSleepIntervalData = object
        types_module.FirebaseSleepMultiContainer = object
        sys.modules["huckleberry_api.firebase_types"] = types_module

        class BrokenCollection:
            def collection(self, *_): return self
            def document(self, *_): return self
            def where(self, **_): return self
            def stream(self):
                async def read():
                    raise ConnectionError("Firestore unavailable")
                    yield None
                return read()

        class API:
            async def _get_firestore_client(self): return BrokenCollection()

        tz = ZoneInfo("America/New_York")
        start = datetime(2026, 9, 27, 18, tzinfo=tz)
        with self.assertRaises(ConnectionError):
            asyncio.run(sync.strict_sleep_intervals(API(), "child", start, start + timedelta(hours=1)))

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

    def test_token_file_is_private_and_rotation_replaces_pair(self):
        original_state = sync.STATE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sync.STATE = Path(tmp) / "nanit_tokens.json"
                sync.save_tokens({"access_token": "first", "refresh_token": "old"})
                sync.save_tokens({"access_token": "second", "refresh_token": "new"})
                self.assertEqual(json.loads(sync.STATE.read_text()),
                                 {"access_token": "second", "refresh_token": "new"})
                self.assertEqual(os.stat(sync.STATE).st_mode & 0o777, 0o600)
        finally:
            sync.STATE = original_state

    def test_restored_client_persists_refreshed_tokens(self):
        original_state, original_client = sync.STATE, sync.NanitClient
        class Manager:
            def on_tokens_refreshed(self, callback):
                self.callback = callback
        class Client:
            def __init__(self, session):
                self.token_manager = Manager()
            def restore_tokens(self, access, refresh):
                self.old_pair = (access, refresh)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sync.STATE = Path(tmp) / "nanit_tokens.json"
                sync.NanitClient = Client
                sync.save_tokens({"access_token": "old-access", "refresh_token": "old-refresh"})
                client = sync.restore_nanit(object())
                self.assertEqual(client.old_pair, ("old-access", "old-refresh"))
                client.token_manager.callback("new-access", "new-refresh")
                self.assertEqual(json.loads(sync.STATE.read_text()),
                                 {"access_token": "new-access", "refresh_token": "new-refresh"})
        finally:
            sync.STATE, sync.NanitClient = original_state, original_client

    def test_existing_manual_sleep_and_last_second_change_block_writes(self):
        tz = ZoneInfo("America/New_York")
        evening = datetime(2020, 9, 27, 20, tzinfo=tz)
        entry = {"type": "auto_sleep", "begin_ts": evening.timestamp(),
                 "end_ts": (evening + timedelta(hours=2)).timestamp()}
        old = types.SimpleNamespace(start=(evening + timedelta(minutes=30)).timestamp(),
                                    duration=1800)

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        class API:
            writes = []
            def __init__(self, **kwargs): pass
            async def authenticate(self): pass
            async def log_sleep(self, *args, **kwargs): self.writes.append((args, kwargs))

        async def calendar(*_): return [entry]
        async def history(*_): return [old]
        env = {"TZ": "America/New_York", "NANIT_BABY_UID": "baby",
               "HUCKLEBERRY_CHILD_UID": "child", "HUCKLEBERRY_EMAIL": "test@example.invalid",
               "HUCKLEBERRY_PASSWORD": "unused", "WRITE_ENABLED": "true"}
        with patch.dict(os.environ, env), patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
             patch.object(sync, "HuckleberryAPI", API), patch.object(sync, "restore_nanit", lambda _: object()), \
             patch.object(sync, "calendar_sleep", calendar), patch.object(sync, "strict_sleep_intervals", history):
            asyncio.run(sync._sync_day(date(2020, 9, 27)))
        self.assertEqual(API.writes, [])

        async def changed_history(*_):
            changed_history.calls += 1
            return [] if changed_history.calls == 1 else [old]
        changed_history.calls = 0
        with patch.dict(os.environ, env), patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
             patch.object(sync, "HuckleberryAPI", API), patch.object(sync, "restore_nanit", lambda _: object()), \
             patch.object(sync, "calendar_sleep", calendar), patch.object(sync, "strict_sleep_intervals", changed_history):
            asyncio.run(sync._sync_day(date(2020, 9, 27)))
        self.assertEqual(changed_history.calls, 2)
        self.assertEqual(API.writes, [])

    def test_calendar_uses_managed_token_and_retries_unauthorized(self):
        class Response:
            def __init__(self, status): self.status = status
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def json(self): return {"calendar": [{"type": "auto_sleep"}]}

        class Manager:
            access_token = "first"
            refreshed = 0
            async def async_get_access_token(self): return self.access_token
            async def async_force_refresh(self, failed_token=None):
                self.refreshed += 1
                self.access_token = "second"

        class Session:
            calls = []
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response(401 if len(self.calls) == 1 else 200)

        client = types.SimpleNamespace(token_manager=Manager(), session=Session())
        start = datetime(2026, 9, 27, tzinfo=ZoneInfo("UTC"))
        result = asyncio.run(sync.calendar_sleep(client, "baby", start, start + timedelta(hours=1)))
        self.assertEqual(result[0]["type"], "auto_sleep")
        self.assertEqual(client.token_manager.refreshed, 1)
        self.assertEqual(client.session.calls[1][1]["headers"]["Authorization"], "token second")
        self.assertEqual(client.session.calls[0][1]["params"]["start"], int(start.timestamp()))


if __name__ == "__main__":
    unittest.main()
