"""Sleep interval and time-window policy."""
import types
import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tests.support import sync


class IntervalsTests(unittest.TestCase):
    def test_huckleberry_fractional_profile_hours(self):
        self.assertEqual(sync.profile_clock(8.0, evening=True), time(20, 0))
        self.assertEqual(sync.profile_clock(7.25), time(7, 15))
        self.assertEqual(sync.profile_clock("20:30", evening=True), time(20, 30))
        with self.assertRaises(ValueError):
            sync.profile_clock(float("nan"), evening=True)

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
