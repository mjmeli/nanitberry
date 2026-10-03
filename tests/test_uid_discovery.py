"""Automatic UID selection and startup discovery."""
import asyncio
import os
import types
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tests.support import sync, SyntheticOwnership


class UidDiscoveryTests(unittest.TestCase):
    def setUp(self):
        ownership_patch = patch.object(sync, "SleepOwnership", SyntheticOwnership)
        ownership_patch.start()
        self.addCleanup(ownership_patch.stop)

    def test_selection_requires_a_single_child(self):
        self.assertEqual(sync.select_uid([("A", "a")], "Nanit"), "a")
        for children in ([], [("A", "a"), ("B", "b")], [("A", "")]):
            with self.subTest(children=children), self.assertRaises(sync.UidSelectionRequired):
                sync.select_uid(children, "Nanit")

    def test_startup_logs_both_accounts_without_uids(self):
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        class API:
            def __init__(self, **kwargs): pass
            async def authenticate(self): pass
            async def get_user(self):
                return types.SimpleNamespace(childList=[
                    types.SimpleNamespace(nickname="H", cid="h-1"),
                    types.SimpleNamespace(nickname="J", cid="h-2")])

        class Nanit:
            async def async_get_babies(self):
                return [types.SimpleNamespace(name="N", uid="n-1")]

        with patch.dict(os.environ, {"CHILD_UID_MAP": "",
                                      "HUCKLEBERRY_EMAIL": "test@example.invalid",
                                      "HUCKLEBERRY_PASSWORD": "unused"}), \
             patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
             patch.object(sync, "HuckleberryAPI", API), \
             patch.object(sync, "restore_nanit", lambda _: Nanit()), \
             self.assertLogs(sync.LOG, level="INFO") as captured:
            asyncio.run(sync.log_missing_uids())
        logs = "\n".join(captured.output)
        for uid in ("n-1", "h-1", "h-2"):
            self.assertIn(uid, logs)
        self.assertIn("set CHILD_UID_MAP", logs)

    def test_sync_uses_single_child_from_each_account(self):
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        class API:
            def __init__(self, **kwargs): pass
            async def authenticate(self): pass
            async def get_user(self):
                return types.SimpleNamespace(childList=[types.SimpleNamespace(nickname="H", cid="h-1")])

        class Nanit:
            async def async_get_babies(self):
                return [types.SimpleNamespace(name="N", uid="n-1")]

        seen = []
        async def calendar(_, uid, *args):
            seen.append(("nanit", uid))
            return []
        async def history(_, uid, *args):
            seen.append(("huckleberry", uid))
            return []

        env = {"TZ": "America/New_York", "CHILD_UID_MAP": "",
               "HUCKLEBERRY_EMAIL": "test@example.invalid", "HUCKLEBERRY_PASSWORD": "unused",
               "USE_HUCKLEBERRY_HOURS": "false"}
        with patch.dict(os.environ, env), \
             patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
             patch.object(sync, "HuckleberryAPI", API), \
             patch.object(sync, "restore_nanit", lambda _: Nanit()), \
             patch.object(sync, "calendar_sleep", calendar), \
             patch.object(sync, "strict_sleep_intervals", history):
            asyncio.run(sync._sync_day(date(2020, 9, 27)))
        self.assertEqual(seen, [("nanit", "n-1"), ("huckleberry", "h-1")])

    def test_missing_nanit_login_does_not_hide_huckleberry_ids(self):
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        class API:
            def __init__(self, **kwargs): pass
            async def authenticate(self): pass
            async def get_user(self):
                return types.SimpleNamespace(childList=[types.SimpleNamespace(nickname="H", cid="h-1")])

        env = {"CHILD_UID_MAP": "",
               "HUCKLEBERRY_EMAIL": "test@example.invalid", "HUCKLEBERRY_PASSWORD": "unused"}
        with patch.dict(os.environ, env), \
             patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
             patch.object(sync, "HuckleberryAPI", API), \
             patch.object(sync, "restore_nanit", side_effect=sync.NanitReauthRequired()), \
             self.assertLogs(sync.LOG, level="INFO") as captured:
            asyncio.run(sync.log_missing_uids())
        logs = "\n".join(captured.output)
        self.assertIn("login is required", logs)
        self.assertIn("h-1", logs)

    def test_map_rejects_ambiguous_or_malformed_pairs(self):
        bad_maps = (
            "{}", "[]", "not JSON", '{"n-1":"h-1","n-2":"h-1"}',
            '{"n-1":"h-1","n-1":"h-2"}', '{"n-1":""}',
        )
        for value in bad_maps:
            with self.subTest(value=value), patch.dict(os.environ, {"CHILD_UID_MAP": value}):
                with self.assertRaises(sync.UidSelectionRequired):
                    sync.configured_uid_pairs()
        with patch.dict(os.environ, {"CHILD_UID_MAP": '{"n-1":"h-1"}'}):
            self.assertEqual(sync.configured_uid_pairs(), [("n-1", "h-1")])
        with patch.dict(os.environ, {"CHILD_UID_MAP": ""}):
            self.assertIsNone(sync.configured_uid_pairs())

    def test_map_writes_each_nanit_child_to_its_matching_huckleberry_child(self):
        evening = datetime(2020, 9, 27, 20, tzinfo=ZoneInfo("America/New_York"))

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        class API:
            writes = []
            def __init__(self, **kwargs): pass
            async def authenticate(self): pass
            async def get_user(self):
                return types.SimpleNamespace(childList=[types.SimpleNamespace(cid="h-1"),
                                                        types.SimpleNamespace(cid="h-2")])
            async def log_sleep(self, uid, **kwargs): self.writes.append(uid)

        class Nanit:
            async def async_get_babies(self):
                return [types.SimpleNamespace(uid="n-1"), types.SimpleNamespace(uid="n-2")]

        seen = []
        async def calendar(_, uid, *args):
            seen.append(("calendar", uid))
            return [{"type": "auto_sleep", "begin_ts": evening.timestamp(),
                     "end_ts": (evening + timedelta(hours=1)).timestamp()}]

        async def history(_, uid, *args):
            seen.append(("history", uid))
            return []

        env = {"TZ": "America/New_York", "CHILD_UID_MAP": '{"n-1":"h-1","n-2":"h-2"}',
               "HUCKLEBERRY_EMAIL": "test@example.invalid", "HUCKLEBERRY_PASSWORD": "unused",
               "WRITE_ENABLED": "true", "SYNC_DAYTIME": "false", "USE_HUCKLEBERRY_HOURS": "false"}
        API.writes = []
        with patch.dict(os.environ, env), \
             patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
             patch.object(sync, "HuckleberryAPI", API), \
             patch.object(sync, "restore_nanit", lambda _: Nanit()), \
             patch.object(sync, "calendar_sleep", calendar), \
             patch.object(sync, "strict_sleep_intervals", history):
            asyncio.run(sync._sync_day(date(2020, 9, 27)))
        self.assertEqual(API.writes, ["h-1", "h-2"])
        self.assertEqual(seen, [("calendar", "n-1"), ("history", "h-1"), ("history", "h-1"),
                                ("calendar", "n-2"), ("history", "h-2"), ("history", "h-2")])

    def test_map_rejects_unknown_uids_before_sync(self):
        class Nanit:
            async def async_get_babies(self):
                return [types.SimpleNamespace(uid="n-1")]

        class API:
            async def get_user(self):
                return types.SimpleNamespace(childList=[types.SimpleNamespace(cid="h-1")])

        for pair in (("n-2", "h-1"), ("n-1", "h-2")):
            with self.subTest(pair=pair), self.assertRaises(sync.UidSelectionRequired):
                asyncio.run(sync.validate_uid_pairs(Nanit(), API(), [pair]))
