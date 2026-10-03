"""Nanit token storage, refresh persistence, and calendar authentication."""
import asyncio
import json
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tests import support
import clients
import config
import storage


class NanitTests(unittest.TestCase):
    def test_token_file_is_private_and_rotation_replaces_pair(self):
        original_state = config.DEFAULT_PATHS.tokens
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config.DEFAULT_PATHS.tokens = Path(tmp) / "nanit_tokens.json"
                storage.save_tokens({"access_token": "first", "refresh_token": "old"})
                storage.save_tokens({"access_token": "second", "refresh_token": "new"})
                self.assertEqual(json.loads(config.DEFAULT_PATHS.tokens.read_text()),
                                 {"access_token": "second", "refresh_token": "new"})
                self.assertEqual(os.stat(config.DEFAULT_PATHS.tokens).st_mode & 0o777, 0o600)
        finally:
            config.DEFAULT_PATHS.tokens = original_state

    def test_restored_client_persists_refreshed_tokens(self):
        original_state, original_client = config.DEFAULT_PATHS.tokens, clients.NanitClient
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
                config.DEFAULT_PATHS.tokens = Path(tmp) / "nanit_tokens.json"
                clients.NanitClient = Client
                storage.save_tokens({"access_token": "old-access", "refresh_token": "old-refresh"})
                client = clients.restore_nanit(object())
                self.assertEqual(client.old_pair, ("old-access", "old-refresh"))
                client.token_manager.callback("new-access", "new-refresh")
                self.assertEqual(json.loads(config.DEFAULT_PATHS.tokens.read_text()),
                                 {"access_token": "new-access", "refresh_token": "new-refresh"})
        finally:
            config.DEFAULT_PATHS.tokens, clients.NanitClient = original_state, original_client

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
        result = asyncio.run(clients.calendar_sleep(client, "baby", start, start + timedelta(hours=1)))
        self.assertEqual(result[0]["type"], "auto_sleep")
        self.assertEqual(client.token_manager.refreshed, 1)
        self.assertEqual(client.session.calls[1][1]["headers"]["Authorization"], "token second")
        self.assertEqual(client.session.calls[0][1]["params"]["start"], int(start.timestamp()))
