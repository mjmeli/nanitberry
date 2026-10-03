"""Sync orchestration, locking, backfill, and scheduling."""
import asyncio
import fcntl
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aionanit import NanitAuthError, NanitConnectionError

import clients
import config
import storage
import intervals
import huckleberry_sleep
import errors
from ownership import SleepOwnership

LOG = logging.getLogger("nanit_huckleberry_sync")


def failure_reason(exc):
    if isinstance(exc, errors.UidSelectionRequired):
        return "uid_selection_required", str(exc)
    if isinstance(exc, (errors.NanitReauthRequired, NanitAuthError)):
        return "nanit_reauth_required", "Run: docker compose run --rm -it nanitberry python sync.py login"
    if isinstance(exc, NanitConnectionError):
        return "nanit_api_error", "Nanit request or token refresh failed; retry later"
    if str(exc).startswith("Nanit calendar"):
        return "nanit_api_error", str(exc)[:200]
    if isinstance(exc, aiohttp.ClientResponseError) and "QUOTA_EXCEEDED" in str(exc):
        return "huckleberry_auth_rate_limited", "Huckleberry password verification is rate limited; retry later"
    if isinstance(exc, aiohttp.ClientResponseError) and exc.status in (400, 401, 403):
        return "huckleberry_auth_rejected", "Check Huckleberry credentials in .env"
    message = str(exc)
    if message.startswith("Nanit"):
        return "nanit_api_error", message
    # Third-party exception text may include HTTP payloads or account data.
    return "sync_error", type(exc).__name__


async def backfill(latest_day, days, *, settings=None, paths=None):
    dates = intervals.backfill_dates(latest_day, days)
    async with aiohttp.ClientSession() as websession:
        context = {"session": websession, "settings": settings or config.Settings.from_env(),
                   "paths": paths or config.DEFAULT_PATHS}
        for day in dates:
            LOG.info("Backfill: processing night starting %s", day)
            await sync_day(day, context)


async def sync_day(day, context=None, *, settings=None, paths=None):
    """Serialize local runs to prevent a scheduled run racing a manual backfill."""
    context = {} if context is None else context
    if "settings" not in context:
        context["settings"] = settings or config.Settings.from_env()
    context.setdefault("paths", paths or config.DEFAULT_PATHS)
    paths = context["paths"]
    paths.tokens.parent.mkdir(parents=True, exist_ok=True)
    with paths.lock.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another sync is running against this data directory") from exc
        storage.status_update("running", "sync_in_progress", last_attempt=storage.utc_now(), night=day.isoformat(), paths=paths)
        try:
            processed = await _sync_day(day, context)
        except Exception as exc:
            reason, detail = failure_reason(exc)
            storage.status_update("error", reason, detail, paths=paths)
            raise
        if not processed:
            storage.status_update("waiting", "window_not_started", "Waiting for the requested sleep period to begin", paths=paths)
            return False
        storage.status_update("ok", "sync_succeeded", last_success=storage.utc_now(), paths=paths)
        return True


async def _sync_day(day, context=None):
    context = {} if context is None else context
    if "session" not in context:
        async with aiohttp.ClientSession() as websession:
            return await _sync_day(day, {**context, "session": websession})
    if "settings" not in context:
        context["settings"] = config.Settings.from_env()
    settings = context["settings"]
    paths = context.setdefault("paths", config.DEFAULT_PATHS)
    if "clients" not in context:
        context["clients"] = await clients.prepare_clients(context["session"], settings=settings, paths=paths)
    nanit, api, pairs = context["clients"]
    return await _sync_day_with_clients(day, nanit, api, pairs, settings=settings, paths=paths)


async def _sync_day_with_clients(day, nanit, api, pairs, now=None, *, settings=None, paths=None):
    settings = settings or config.Settings.from_env()
    tz = ZoneInfo(settings.timezone)
    gap = int(settings.max_wake_minutes)
    if not 0 <= gap <= 180:
        raise ValueError("MAX_WAKE_MINUTES must be between 0 and 180")
    write = settings.write_enabled
    include_day = settings.sync_daytime
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    ownership = SleepOwnership(now_ts, paths=paths)
    if write:
        ownership.save()  # Persist retention pruning even when nothing is ready.
    all_ready = True
    for nanit_uid, huckleberry_uid in pairs:
        LOG.info("Syncing Nanit %s to Huckleberry %s", nanit_uid, huckleberry_uid)
        if settings.use_huckleberry_hours:
            child = await api.get_child(huckleberry_uid)
            if child is None or child.nightStart is None or child.morningCutoff is None:
                raise RuntimeError("Huckleberry nightStart/morningCutoff unavailable; configure explicit hours")
            night_start = intervals.profile_clock(child.nightStart, evening=True)
            morning_cutoff = intervals.profile_clock(child.morningCutoff)
        else:
            night_start = intervals.parse_clock(settings.night_start)
            morning_cutoff = intervals.parse_clock(settings.morning_cutoff)
        ranges = intervals.windows(day, tz, night_start, morning_cutoff, include_day)
        evening = ranges[-1][1]
        morning = ranges[-1][2]
        next_evening = intervals.local_boundary(day + timedelta(days=1), night_start, tz)
        # Include daytime before this evening to preserve sleep crossing night start.
        day_start = intervals.local_boundary(day, morning_cutoff, tz)
        if day_start.timestamp() >= now_ts:
            all_ready = False
            continue
        # Include a pre-cutoff segment that may join a daytime continuation.
        fetch_start = datetime.fromtimestamp(day_start.timestamp() - gap * 60, tz)
        # Look past the next evening far enough to discover a short-wake continuation.
        fetch_end = datetime.fromtimestamp(
            min(next_evening.timestamp() + gap * 60, now_ts), tz)
        calendar = await clients.calendar_sleep(nanit, nanit_uid, fetch_start, fetch_end)
        completed = intervals.completed_calendar_entries(calendar, now_ts)
        # This strict read propagates failures. The library's list_sleep_intervals
        # catches some Firestore errors and otherwise returns an unsafe empty list.
        existing = [intervals.existing_range(x) for x in await huckleberry_sleep.strict_sleep_intervals(
            api, huckleberry_uid, fetch_start, fetch_end)]
        # Keep actual boundaries so a cross-cutoff sleep cannot become a daytime nap.
        spans = intervals.normalize(completed, fetch_start, fetch_end, gap * 60, clip=False)
        # Import available bounds immediately. Open continuations have no invented
        # end; later polls reconcile the owned row as more source data arrives.
        spans = [span for span in spans if span[1].timestamp() <= now_ts]
        spans_by_kind = intervals.classify_spans(spans, day_start, evening, morning, include_day)
        for kind, spans in spans_by_kind:
            LOG.info("%s %s: %d Nanit interval(s)", day, kind, len(spans))
            for span in spans:
                if (not ownership.candidates(nanit_uid, huckleberry_uid, span)
                        and any(intervals.overlap(span, old) for old in existing)):
                    LOG.warning("Skipping %s–%s: overlaps existing Huckleberry sleep", *span)
                    continue
                await ownership.sync_span(api, nanit_uid, huckleberry_uid, span,
                                          write=write, kind=kind)
    return all_ready


async def scheduled(*, settings=None, paths=None):
    settings = settings or config.Settings.from_env()
    tz = ZoneInfo(settings.timezone)
    async with aiohttp.ClientSession() as websession:
        context = {"session": websession, "settings": settings,
                   "paths": paths or config.DEFAULT_PATHS}
        while True:
            try:
                today = datetime.now(tz).date()
                # Cover the night across midnight and today's daytime/evening sleep.
                for day in (today - timedelta(days=1), today):
                    await sync_day(day, context)
            except Exception:
                LOG.exception("Scheduled sync failed; will retry at the next scheduled check")
            now_ts = datetime.now(timezone.utc).timestamp()
            next_tick = (int(now_ts) // (15 * 60) + 1) * (15 * 60)
            LOG.info("Next sync check at %s", datetime.fromtimestamp(next_tick, tz))
            await asyncio.sleep(max(1, next_tick - now_ts))

