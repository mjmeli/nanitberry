"""Module integration, explicit run dependencies, and the preserved CLI."""
import asyncio
import io
import json
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import AsyncMock, patch

from tests import support
import clients
import config
import service
import storage
import huckleberry_sleep
import sync


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class RefactorTests(unittest.TestCase):
    def test_backfill_carries_explicit_settings_and_paths_through_all_modules(self):
        """Real preparation, locking, health, and journal use the supplied directory."""
        settings = config.Settings(
            timezone="UTC", use_huckleberry_hours=False, write_enabled=True,
            night_start="20:00", morning_cutoff="07:00",
            child_uid_map='{"baby":"child"}',
            huckleberry_email="test@example.com", huckleberry_password="test-password",
        )
        prepared = []

        class Nanit:
            def __init__(self, session):
                self.token_manager = types.SimpleNamespace(on_tokens_refreshed=self.set_callback)
                prepared.append(self)

            def set_callback(self, callback):
                self.refresh = callback

            def restore_tokens(self, access, refresh):
                self.tokens = (access, refresh)

            async def async_get_babies(self):
                return [types.SimpleNamespace(uid="baby")]

        class Huckleberry:
            authentications = 0

            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def authenticate(self):
                self.__class__.authentications += 1
                self.assert_credentials()

            def assert_credentials(self):
                if self.kwargs["email"] != settings.huckleberry_email:
                    raise AssertionError("Explicit credentials were ignored")

            async def get_user(self):
                return types.SimpleNamespace(childList=[types.SimpleNamespace(cid="child")])

        with tempfile.TemporaryDirectory() as tmp:
            paths = config.StoragePaths.for_directory(tmp)
            storage.save_tokens({"access_token": "old", "refresh_token": "refresh"}, paths=paths)
            with patch.dict("os.environ", {"TZ": "invalid", "CHILD_UID_MAP": "invalid",
                                           "WRITE_ENABLED": "false"}), \
                 patch.object(clients.aiohttp, "ClientSession", Session, create=True), \
                 patch.object(clients, "NanitClient", Nanit), \
                 patch.object(clients, "HuckleberryAPI", Huckleberry), \
                 patch.object(clients, "calendar_sleep", AsyncMock(return_value=[])) as calendar, \
                 patch.object(huckleberry_sleep, "strict_sleep_intervals", AsyncMock(return_value=[])):
                asyncio.run(service.backfill(date(2020, 9, 27), 2, settings=settings, paths=paths))
            self.assertEqual(Huckleberry.authentications, 1)
            self.assertEqual(len(prepared), 1)
            self.assertEqual(calendar.await_count, 2)
            self.assertEqual(json.loads(paths.ownership.read_text()), {"version": 1, "records": {}})
            self.assertEqual(json.loads(paths.status.read_text())["night"], "2020-09-27")
            self.assertTrue(paths.lock.exists())
            self.assertTrue(storage.health_status(paths=paths)[0])
            prepared[0].refresh("rotated-access", "rotated-refresh")
            self.assertEqual(json.loads(paths.tokens.read_text())["access_token"], "rotated-access")

    def test_huckleberry_refresh_callback_keeps_explicit_storage_path(self):
        class API:
            email = "test@example.com"
            refresh_token = "old"
            user_uid = "user"

            async def refresh_session_token(self):
                self.refresh_token += "-rotated"

            async def authenticate(self):
                raise AssertionError("A cached refresh token should be used")

        with tempfile.TemporaryDirectory() as tmp:
            paths = config.StoragePaths.for_directory(tmp)
            api = API()
            storage.save_huckleberry_token(api, paths=paths)
            asyncio.run(clients.authenticate_huckleberry(api, paths=paths))
            asyncio.run(api.refresh_session_token())
            cached = json.loads(paths.huckleberry_tokens.read_text())
            self.assertEqual(cached["refresh_token"], "old-rotated-rotated")

    def test_cli_routes_existing_commands(self):
        routes = [
            (["login"], clients, "nanit_login", ()),
            (["children"], clients, "huckleberry_children", ()),
            (["once", "--date", "2020-09-27"], service, "sync_day", (date(2020, 9, 27),)),
            (["backfill", "--date", "2020-09-27", "--days", "3"], service, "backfill",
             (date(2020, 9, 27), 3)),
            (["serve"], service, "scheduled", ()),
        ]
        settings = config.Settings()
        with tempfile.TemporaryDirectory() as tmp:
            paths = config.StoragePaths.for_directory(tmp)
            for argv, module, name, args in routes:
                with self.subTest(command=argv[0]), \
                     patch.object(sync.sys, "argv", ["sync.py", *argv]), \
                     patch.object(config.Settings, "from_env", return_value=settings), \
                     patch.object(config, "DEFAULT_PATHS", paths), \
                     patch.object(clients, "log_missing_uids", AsyncMock()), \
                     patch.object(module, name, AsyncMock()) as action:
                    sync.main()
                    action.assert_awaited_once_with(*args, settings=settings, paths=paths)

    def test_cli_babies_status_and_healthcheck(self):
        nanit = types.SimpleNamespace(async_get_babies=AsyncMock(return_value=[
            types.SimpleNamespace(name="Baby", uid="baby-uid")]))
        with tempfile.TemporaryDirectory() as tmp:
            paths = config.StoragePaths.for_directory(tmp)
            storage.save_tokens({"access_token": "access", "refresh_token": "refresh"}, paths=paths)
            storage.status_update("ok", "sync_succeeded", last_success=storage.utc_now(), paths=paths)
            with patch.object(config, "DEFAULT_PATHS", paths):
                output = io.StringIO()
                with patch.object(sync.sys, "argv", ["sync.py", "babies"]), \
                     patch.object(clients.aiohttp, "ClientSession", Session, create=True), \
                     patch.object(clients, "restore_nanit", return_value=nanit), redirect_stdout(output):
                    sync.main()
                self.assertEqual(output.getvalue().strip(), "Baby baby-uid")
                output = io.StringIO()
                with patch.object(sync.sys, "argv", ["sync.py", "status"]), redirect_stdout(output):
                    sync.main()
                self.assertTrue(json.loads(output.getvalue())["healthy"])
                storage.status_update("error", "sync_error", paths=paths)
                with patch.object(sync.sys, "argv", ["sync.py", "healthcheck"]), \
                     redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit:
                    sync.main()
                self.assertEqual(exit.exception.code, 1)
