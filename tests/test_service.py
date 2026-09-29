"""End-to-end orchestration using synthetic API responses."""
import asyncio
import json
import os
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tests.support import sync


class ServiceTests(unittest.TestCase):
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

        env = {"TZ": "America/New_York", "NANIT_BABY_UID": "baby",
               "HUCKLEBERRY_CHILD_UID": "child", "HUCKLEBERRY_EMAIL": "test@example.invalid",
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
        env = {"TZ": "America/New_York", "NANIT_BABY_UID": "baby",
               "HUCKLEBERRY_CHILD_UID": "child", "HUCKLEBERRY_EMAIL": "test@example.invalid",
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
                     patch.object(sync, "calendar_sleep", calendar), \
                     patch.object(sync, "strict_sleep_intervals", history):
                    asyncio.run(sync.sync_day(date(2020, 9, 27)))
                self.assertEqual(API.writes, [])
                self.assertEqual(json.loads(sync.STATUS.read_text())["state"], "ok")
                self.assertTrue(sync.health_status()[0])
        finally:
            sync.STATE, sync.STATUS = original_state, original_status
