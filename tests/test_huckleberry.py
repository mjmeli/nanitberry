"""Huckleberry history reads, child lookup, and duplicate protection."""
import asyncio
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from io import StringIO
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from tests.support import sync


class HuckleberryTests(unittest.TestCase):
    def test_failed_history_read_stops_sync(self):
        google = types.ModuleType("google")
        cloud = types.ModuleType("google.cloud")
        firestore = types.ModuleType("google.cloud.firestore")
        firestore.FieldFilter = lambda *args: args
        cloud.firestore = firestore
        google.cloud = cloud
        types_module = types.ModuleType("huckleberry_api.firebase_types")
        types_module.FirebaseSleepIntervalData = object
        types_module.FirebaseSleepMultiContainer = object

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
        with patch.dict(sys.modules, {"google": google, "google.cloud": cloud,
                                      "google.cloud.firestore": firestore,
                                      "huckleberry_api.firebase_types": types_module}):
            with self.assertRaises(ConnectionError):
                asyncio.run(sync.strict_sleep_intervals(API(), "child", start, start + timedelta(hours=1)))

    def test_history_includes_regular_and_batched_entries_crossing_window_start(self):
        firestore = types.ModuleType("google.cloud.firestore")
        firestore.FieldFilter = lambda *args: args
        cloud = types.ModuleType("google.cloud")
        cloud.firestore = firestore
        google = types.ModuleType("google")
        google.cloud = cloud

        class Interval:
            @classmethod
            def model_validate(cls, data):
                return types.SimpleNamespace(**data)

        class Multi:
            @classmethod
            def model_validate(cls, data):
                return types.SimpleNamespace(data={
                    key: Interval.model_validate(value) for key, value in data["data"].items()})

        models = types.ModuleType("huckleberry_api.firebase_types")
        models.FirebaseSleepIntervalData = Interval
        models.FirebaseSleepMultiContainer = Multi
        regular_rows = [
            {"start": 800, "duration": 100},  # ended before the window
            {"start": 900, "duration": 200},  # begins before, overlaps
            {"start": 1900, "duration": 200},
            {"start": 2100, "duration": 100},  # filtered by Firestore
        ]
        multi_rows = [{"multi": True, "data": {
            "old": {"start": 700, "duration": 100},
            "crossing": {"start": 950, "duration": 100},
            "late": {"start": 2000, "duration": 100},
        }}]
        filters = []

        class Collection:
            def collection(self, *_): return self
            def document(self, *_): return self
            def where(self, *, filter):
                filters.append(filter)
                rows = ([row for row in regular_rows if row["start"] < filter[2]]
                        if filter[0] == "start" else multi_rows)

                class Query:
                    def stream(self):
                        async def documents():
                            for row in rows:
                                yield types.SimpleNamespace(to_dict=lambda row=row: row)
                        return documents()
                return Query()

        class API:
            async def _get_firestore_client(self): return Collection()

        start = datetime.fromtimestamp(1000, ZoneInfo("UTC"))
        end = datetime.fromtimestamp(2000, ZoneInfo("UTC"))
        with patch.dict(sys.modules, {"google": google, "google.cloud": cloud,
                                      "google.cloud.firestore": firestore,
                                      "huckleberry_api.firebase_types": models}):
            found = asyncio.run(sync.strict_sleep_intervals(API(), "child", start, end))
        self.assertEqual(sorted(entry.start for entry in found), [900, 950, 1900])
        self.assertEqual(filters, [("start", "<", 2000.0), ("multi", "==", True)])
        self.assertTrue(any(sync.overlap(sync.existing_range(entry),
                                         (start, datetime.fromtimestamp(1050, ZoneInfo("UTC"))))
                            for entry in found))

    def test_huckleberry_children_uses_account_child_cids(self):
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        class API:
            def __init__(self, **kwargs): pass
            async def authenticate(self): pass
            async def get_user(self):
                return types.SimpleNamespace(childList=[
                    types.SimpleNamespace(nickname="Example", cid="child-123")])

        output = StringIO()
        env = {"HUCKLEBERRY_EMAIL": "test@example.invalid",
               "HUCKLEBERRY_PASSWORD": "unused"}
        with patch.dict(os.environ, env), patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
             patch.object(sync, "HuckleberryAPI", API), redirect_stdout(output):
            asyncio.run(sync.huckleberry_children())
        self.assertEqual(output.getvalue(), "Example\tchild-123\n")

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
        env = {"TZ": "America/New_York", "CHILD_UID_MAP": '{"baby":"child"}',
               "HUCKLEBERRY_EMAIL": "test@example.invalid",
               "HUCKLEBERRY_PASSWORD": "unused", "WRITE_ENABLED": "true"}
        with patch.dict(os.environ, env), patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
             patch.object(sync, "HuckleberryAPI", API), patch.object(sync, "restore_nanit", lambda _: object()), \
             patch.object(sync, "validate_uid_pairs", new_callable=AsyncMock), \
             patch.object(sync, "calendar_sleep", calendar), patch.object(sync, "strict_sleep_intervals", history):
            asyncio.run(sync._sync_day(date(2020, 9, 27)))
        self.assertEqual(API.writes, [])

        async def changed_history(*_):
            changed_history.calls += 1
            return [] if changed_history.calls == 1 else [old]
        changed_history.calls = 0
        with patch.dict(os.environ, env), patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
             patch.object(sync, "HuckleberryAPI", API), patch.object(sync, "restore_nanit", lambda _: object()), \
             patch.object(sync, "validate_uid_pairs", new_callable=AsyncMock), \
             patch.object(sync, "calendar_sleep", calendar), patch.object(sync, "strict_sleep_intervals", changed_history):
            asyncio.run(sync._sync_day(date(2020, 9, 27)))
        self.assertEqual(changed_history.calls, 2)
        self.assertEqual(API.writes, [])
