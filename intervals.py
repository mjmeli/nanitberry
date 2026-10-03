"""Sleep interval normalization, classification, and local time boundaries."""
import logging
import math
import re
from datetime import datetime, time as clock, timedelta, timezone

LOG = logging.getLogger("nanit_huckleberry_sync")


def parse_clock(value):
    if not re.fullmatch(r"\d{1,2}:\d{2}", str(value)):
        raise ValueError(f"Expected HH:MM, got {value!r}")
    hour, minute = map(int, str(value).split(":"))
    return clock(hour, minute)


def profile_clock(value, evening=False):
    """Parse Huckleberry's HH:MM or fractional-hour profile boundaries."""
    if isinstance(value, bool):
        raise ValueError("Huckleberry boundary must be a clock time")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or not 0 <= value < 24:
            raise ValueError("Huckleberry boundary must be between 0 and 24 hours")
        total_minutes = round(value * 60)
        if not 0 <= total_minutes < 24 * 60:
            raise ValueError("Huckleberry boundary must be before 24:00")
        result = clock(total_minutes // 60, total_minutes % 60)
    else:
        result = parse_clock(value)
    # Huckleberry stores an evening value such as 8.0 for 8 p.m.
    if evening and 1 <= result.hour < 12:
        result = clock(result.hour + 12, result.minute)
    return result


def normalize(entries, start, end, max_gap_seconds, *, clip=True):
    """Deduplicate and join segments, optionally clipping to the query window."""
    parts = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Nanit calendar contains a non-object entry")
        if entry.get("type") != "auto_sleep":
            continue
        try:
            a, b = float(entry["begin_ts"]), float(entry["end_ts"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Nanit auto_sleep has invalid timestamps") from exc
        if not math.isfinite(a) or not math.isfinite(b) or b <= a:
            raise ValueError("Nanit auto_sleep has invalid timestamps")
        if clip:
            a, b = max(a, start.timestamp()), min(b, end.timestamp())
        if a < b:
            parts.append((a, b))
    parts.sort()
    merged = []
    for a, b in parts:
        if merged and a - merged[-1][1] <= max_gap_seconds:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return [(datetime.fromtimestamp(a, start.tzinfo), datetime.fromtimestamp(b, start.tzinfo)) for a, b in merged]


def completed_calendar_entries(entries, now_ts):
    """Leave a recent auto_sleep without an end for the next poll."""
    completed = []
    for entry in entries:
        if (isinstance(entry, dict) and entry.get("type") == "auto_sleep"
                and entry.get("end_ts") is None):
            try:
                begin = float(entry["begin_ts"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Nanit auto_sleep has invalid timestamps") from exc
            if not math.isfinite(begin) or not now_ts - 36 * 3600 <= begin <= now_ts:
                raise ValueError("Nanit auto_sleep has invalid timestamps")
            LOG.info("Nanit auto_sleep beginning %s is still in progress",
                     datetime.fromtimestamp(begin, timezone.utc))
            continue
        completed.append(entry)
    return completed


def overlap(first, second):
    # Python compares wall times for two datetimes with the same ZoneInfo,
    # which gives the wrong result in the repeated hour at fall DST change.
    return first[0].timestamp() < second[1].timestamp() and second[0].timestamp() < first[1].timestamp()


def existing_range(interval):
    a, duration = float(interval.start), float(interval.duration)
    if not math.isfinite(a) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Huckleberry sleep has invalid timestamps")
    return datetime.fromtimestamp(a, timezone.utc), datetime.fromtimestamp(a + duration, timezone.utc)


def local_boundary(day, hour, tz):
    boundary = datetime.combine(day, hour, tz)
    roundtrip = boundary.astimezone(timezone.utc).astimezone(tz)
    if roundtrip.replace(tzinfo=None) != boundary.replace(tzinfo=None):
        raise ValueError(f"Local time {day} {hour} does not exist in {tz}")
    if boundary.replace(fold=1).utcoffset() != boundary.utcoffset():
        raise ValueError(f"Local time {day} {hour} is ambiguous in {tz}")
    return boundary


def windows(day, tz, night_start, morning_cutoff, include_day):
    if night_start <= morning_cutoff:
        raise ValueError("NIGHT_START must be later in the day than MORNING_CUTOFF")
    evening = local_boundary(day, night_start, tz)
    morning = local_boundary(day + timedelta(days=1), morning_cutoff, tz)
    result = [("night", evening, morning)]
    if include_day:
        result.insert(0, ("day", local_boundary(day, morning_cutoff, tz), evening))
    return result


def backfill_dates(latest_day, days):
    if not 1 <= days <= 365:
        raise ValueError("Backfill days must be between 1 and 365")
    return [latest_day - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def classify_spans(spans, day_start, evening, morning, include_day):
    """Assign whole intervals to day or night using instant-based boundaries."""
    spans_by_kind = []
    if include_day:
        # A span crossing the evening boundary belongs wholly to the night.
        day_spans = [span for span in spans
                     if span[0].timestamp() >= day_start.timestamp()
                     and span[1].timestamp() <= evening.timestamp()]
        spans_by_kind.append(("day", day_spans))
    night_spans = [span for span in spans
                   if span[0].timestamp() < morning.timestamp()
                   and span[1].timestamp() > evening.timestamp()]
    spans_by_kind.append(("night", night_spans))
    return spans_by_kind
