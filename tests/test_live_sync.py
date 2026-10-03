"""Readiness and classification while a sleep window is still underway."""
import asyncio
import os
import types
import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from tests.support import sync


class LiveSyncTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("America/New_York")
        self.entries = []
        self.writes = []
        self.env = {"TZ": "America/New_York", "USE_HUCKLEBERRY_HOURS": "false",
                    "NIGHT_START": "20:00", "MORNING_CUTOFF": "07:00",
                    "MAX_WAKE_MINUTES": "20", "SYNC_DAYTIME": "false",
                    "WRITE_ENABLED": "true"}

    def at(self, day, hour, minute=0):
        return datetime(2026, 10, day, hour, minute, tzinfo=self.tz)

    def add_sleep(self, start, end=None):
        self.entries.append({"type": "auto_sleep", "begin_ts": start.timestamp(),
                             "end_ts": end.timestamp() if end else None})

    def poll(self, day, now):
        async def calendar(_, __, start, end):
            # Model a calendar returning records overlapping the query window.
            return [entry for entry in self.entries
                    if entry["begin_ts"] < end.timestamp()
                    and (entry["end_ts"] is None or entry["end_ts"] > start.timestamp())]

        async def history(*_):
            return [types.SimpleNamespace(start=a.timestamp(),
                                          duration=b.timestamp() - a.timestamp())
                    for a, b in self.writes]

        async def log_sleep(_, start_time, end_time):
            self.writes.append((start_time, end_time))

        api = types.SimpleNamespace(log_sleep=log_sleep)
        with patch.dict(os.environ, self.env), \
             patch.object(sync, "calendar_sleep", calendar), \
             patch.object(sync, "strict_sleep_intervals", history):
            asyncio.run(sync._sync_day_with_clients(
                date(2026, 10, day), object(), api, [("baby", "child")], now=now))

    def test_night_imports_after_gap_before_morning_without_duplicates(self):
        first = (self.at(1, 20), self.at(2, 1))
        second = (self.at(2, 1, 30), self.at(2, 7))
        self.add_sleep(*first)
        self.poll(1, self.at(2, 1, 15))
        self.assertEqual(self.writes, [])
        self.poll(1, self.at(2, 1, 20))
        self.assertEqual(self.writes, [first])
        self.add_sleep(*second)
        self.poll(1, self.at(2, 7))
        self.assertEqual(self.writes, [first])
        self.poll(1, self.at(2, 7, 30))
        self.poll(2, self.at(2, 7, 30))
        self.assertEqual(self.writes, [first, second])

    def test_evening_before_midnight_imports_on_current_date(self):
        span = (self.at(1, 20), self.at(1, 21))
        self.add_sleep(*span)
        self.poll(1, self.at(1, 21, 30))
        self.assertEqual(self.writes, [span])

    def test_daytime_requires_enablement_and_same_gap(self):
        span = (self.at(2, 10), self.at(2, 11))
        self.add_sleep(*span)
        self.poll(2, self.at(2, 11, 30))
        self.assertEqual(self.writes, [])
        self.env["SYNC_DAYTIME"] = "true"
        self.poll(2, self.at(2, 11, 15))
        self.assertEqual(self.writes, [])
        self.poll(2, self.at(2, 11, 20))
        self.assertEqual(self.writes, [span])

    def test_active_short_wake_continuation_keeps_earlier_sleep_pending(self):
        self.add_sleep(self.at(1, 20), self.at(2, 1))
        self.add_sleep(self.at(2, 1, 10))
        self.poll(1, self.at(2, 1, 30))
        self.assertEqual(self.writes, [])
        self.entries[-1]["end_ts"] = self.at(2, 2).timestamp()
        self.poll(1, self.at(2, 2, 20))
        self.assertEqual(self.writes, [(self.at(1, 20), self.at(2, 2))])

    def test_short_wake_crossing_cutoff_stays_night_in_both_date_queries(self):
        self.env["SYNC_DAYTIME"] = "true"
        self.add_sleep(self.at(1, 23), self.at(2, 6, 55))
        self.add_sleep(self.at(2, 7, 5), self.at(2, 7, 40))
        # Today's query must not import the continuation as a daytime entry.
        self.poll(2, self.at(2, 8))
        self.assertEqual(self.writes, [])
        self.poll(1, self.at(2, 8))
        self.assertEqual(self.writes, [(self.at(1, 23), self.at(2, 7, 40))])

    def test_zero_gap_still_waits_for_sleep_to_finish(self):
        self.env["MAX_WAKE_MINUTES"] = "0"
        self.add_sleep(self.at(1, 20))
        self.poll(1, self.at(1, 21))
        self.assertEqual(self.writes, [])
        self.entries[-1]["end_ts"] = self.at(1, 21).timestamp()
        self.poll(1, self.at(1, 21))
        self.assertEqual(self.writes, [(self.at(1, 20), self.at(1, 21))])

    def test_daytime_short_wake_crossing_evening_waits_and_becomes_night(self):
        self.env["SYNC_DAYTIME"] = "true"
        self.add_sleep(self.at(1, 19), self.at(1, 19, 50))
        self.add_sleep(self.at(1, 20))
        self.poll(1, self.at(1, 20, 15))
        self.assertEqual(self.writes, [])
        self.entries[-1]["end_ts"] = self.at(1, 21).timestamp()
        # The combined interval imports even with daytime tracking disabled.
        self.env["SYNC_DAYTIME"] = "false"
        self.poll(1, self.at(1, 21, 30))
        self.assertEqual(self.writes, [(self.at(1, 19), self.at(1, 21))])

    def test_long_wake_after_cutoff_creates_optional_daytime_entry(self):
        first = (self.at(1, 23), self.at(2, 6, 55))
        second = (self.at(2, 7, 30), self.at(2, 8))
        self.add_sleep(*first)
        self.add_sleep(*second)
        self.poll(1, self.at(2, 8, 30))
        self.poll(2, self.at(2, 8, 30))
        self.assertEqual(self.writes, [first])
        self.env["SYNC_DAYTIME"] = "true"
        self.poll(2, self.at(2, 8, 30))
        self.assertEqual(self.writes, [first, second])

    def test_scheduler_checks_previous_and_current_dates_on_each_tick(self):
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass

        class StopScheduler(Exception):
            pass

        run = AsyncMock()
        with patch.dict(os.environ, self.env), \
             patch.object(sync.aiohttp, "ClientSession", Session, create=True), \
             patch.object(sync, "datetime", wraps=datetime) as clock, \
             patch.object(sync, "sync_day", run), \
             patch.object(sync.asyncio, "sleep", AsyncMock(side_effect=[None, StopScheduler])):
            clock.now.return_value = self.at(2, 1, 30)
            with self.assertRaises(StopScheduler):
                asyncio.run(sync.scheduled())
        self.assertEqual([call.args[0] for call in run.await_args_list],
                         [date(2026, 10, 1), date(2026, 10, 2)])
