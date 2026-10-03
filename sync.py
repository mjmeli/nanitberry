"""Import Nanit calendar sleep into Huckleberry after a completed night."""
import argparse
import asyncio
import fcntl
import json
import logging
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, time as clock, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aionanit import NanitAuthError, NanitClient, NanitConnectionError, NanitMfaRequiredError
from huckleberry_api import HuckleberryAPI

LOG = logging.getLogger("nanit_huckleberry_sync")
API = "https://api.nanit.com"
STATE = Path("/data/nanit_tokens.json")
STATUS = STATE.with_name("sync_status.json")


class NanitReauthRequired(RuntimeError):
    """A fresh interactive MFA login is required."""


class UidSelectionRequired(RuntimeError):
    """An account has no unambiguous child to sync."""


def utc_now():
    return datetime.now(ZoneInfo("UTC")).isoformat()


def status_update(state, reason, detail="", **extra):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(STATUS.read_text()) if STATUS.exists() else {}
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    report = {**previous, "state": state, "reason": reason, "detail": detail,
              "last_update": utc_now(), **extra}
    private_json(STATUS, report, indent=2)


def private_json(path, data, **kwargs):
    """Replace a credential or status file without a world-readable creation window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, **kwargs)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def failure_reason(exc):
    if isinstance(exc, UidSelectionRequired):
        return "uid_selection_required", str(exc)
    if isinstance(exc, (NanitReauthRequired, NanitAuthError)):
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


def health_status():
    if not STATE.exists():
        return False, "nanit_reauth_required: run interactive login"
    try:
        tokens = json.loads(STATE.read_text())
        if not isinstance(tokens, dict) or not all(
            isinstance(tokens.get(key), str) and tokens[key]
            for key in ("access_token", "refresh_token")
        ):
            raise ValueError("incomplete token file")
    except (OSError, ValueError):
        return False, "nanit_reauth_required: token file is unreadable or incomplete"
    try:
        report = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return False, "no_sync_status: run a dry run or wait for the first scheduled run"
    if not isinstance(report, dict):
        return False, "invalid_sync_status"
    if report.get("state") == "error":
        return False, f"{report.get('reason')}: {report.get('detail', '')}"
    max_age = int(setting("HEALTH_MAX_AGE_HOURS", "36"))
    anchor = report.get("last_success")
    if not anchor:
        return False, "no_successful_run: run a dry run or start the scheduler"
    try:
        success = datetime.fromisoformat(anchor)
        if success.tzinfo is None:
            raise ValueError("naive timestamp")
        age = datetime.now(timezone.utc) - success
    except (TypeError, ValueError):
        return False, "invalid_sync_status"
    if age < -timedelta(minutes=5):
        return False, "invalid_sync_status: success time is in the future"
    if age > timedelta(hours=max_age):
        return False, f"sync_stale: no successful run in {max_age} hours"
    return True, f"{report.get('state')}: last success {report.get('last_success', 'pending')}"


def setting(name, default):
    return os.getenv(name, default)


def boolean(name, default=False):
    return setting(name, str(default)).lower() in ("true", "yes", "1", "on")


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


def save_tokens(data):
    tokens = {key: data.get(key) for key in ("access_token", "refresh_token")}
    if not all(tokens.values()):
        raise RuntimeError("Nanit did not return access and refresh tokens")
    private_json(STATE, tokens)


def huckleberry_token_path():
    return STATE.with_name("huckleberry_tokens.json")


def save_huckleberry_token(api):
    if not api.refresh_token or not api.user_uid:
        raise RuntimeError("Huckleberry did not return a refresh token and user UID")
    private_json(huckleberry_token_path(), {
        "email": api.email,
        "refresh_token": api.refresh_token,
        "user_uid": api.user_uid,
    })


async def authenticate_huckleberry(api):
    """Reuse a saved Firebase refresh token across short-lived containers."""
    refresh = getattr(api, "refresh_session_token", None)
    if refresh is None:
        # Synthetic API objects used by local tests do not manage tokens.
        await api.authenticate()
        return

    async def refresh_and_save():
        await refresh()
        save_huckleberry_token(api)

    api.refresh_session_token = refresh_and_save
    try:
        cached = json.loads(huckleberry_token_path().read_text())
    except (OSError, ValueError):
        cached = None
    if (isinstance(cached, dict) and cached.get("email") == api.email
            and isinstance(cached.get("refresh_token"), str) and cached["refresh_token"]
            and isinstance(cached.get("user_uid"), str) and cached["user_uid"]):
        api.refresh_token = cached["refresh_token"]
        api.user_uid = cached["user_uid"]
        try:
            await api.refresh_session_token()
            return
        except aiohttp.ClientResponseError as exc:
            if not any(code in str(exc) for code in ("INVALID_REFRESH_TOKEN", "TOKEN_EXPIRED")):
                raise
            LOG.warning("Saved Huckleberry refresh token was rejected; trying password login")
    await api.authenticate()
    save_huckleberry_token(api)


async def nanit_login():
    email, password = os.getenv("NANIT_EMAIL"), os.getenv("NANIT_PASSWORD")
    if not email or not password:
        raise RuntimeError("Set NANIT_EMAIL and NANIT_PASSWORD for the one-time login")
    async with aiohttp.ClientSession() as session:
        client = NanitClient(session)
        try:
            result = await client.async_login(email, password)
        except NanitMfaRequiredError as exc:
            if not sys.stdin.isatty():
                raise RuntimeError("Nanit MFA requires an interactive terminal: docker compose run --rm -it sync python sync.py login") from exc
            code = input("Nanit MFA code: ").strip()
            result = await client.async_verify_mfa(email, password, exc.mfa_token, code)
    save_tokens(result)
    status_update("waiting", "login_complete", "Waiting for first sync", service_started=utc_now())
    LOG.info("Nanit login succeeded; refresh credentials stored at %s", STATE)


async def huckleberry_children():
    """List the child IDs available to the configured Huckleberry account."""
    async with aiohttp.ClientSession() as session:
        api = HuckleberryAPI(
            email=os.environ["HUCKLEBERRY_EMAIL"],
            password=os.environ["HUCKLEBERRY_PASSWORD"],
            timezone=setting("TZ", "America/New_York"),
            websession=session,
        )
        await authenticate_huckleberry(api)
        user = await api.get_user()
        if user is None:
            raise RuntimeError("Huckleberry user profile was not found")
        for child in user.childList:
            print(f"{child.nickname or '(unnamed)'}\t{child.cid}")


def select_uid(items, account):
    """Log available children and select one only when unambiguous."""
    for name, uid in items:
        LOG.info("Available %s child: %s (UID: %s)", account, name, uid)
    if len(items) == 1:
        if not isinstance(items[0][1], str) or not items[0][1].strip():
            raise UidSelectionRequired(f"The only {account} child has no usable UID")
        LOG.info("Using the only %s child", account)
        return items[0][1]
    if not items:
        raise UidSelectionRequired(f"No {account} children found; check account access")
    raise UidSelectionRequired(
        f"Multiple {account} children found; set CHILD_UID_MAP with matching UIDs shown above")


def configured_uid_pairs():
    """Return explicit Nanit-to-Huckleberry pairs, if configured."""
    raw = os.getenv("CHILD_UID_MAP", "").strip()
    if not raw:
        return None
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate Nanit UID")
            result[key] = value
        return result

    try:
        mapping = json.loads(raw, object_pairs_hook=unique_keys)
    except ValueError as exc:
        raise UidSelectionRequired("CHILD_UID_MAP must be a JSON object with unique Nanit UIDs") from exc
    if (not isinstance(mapping, dict) or not mapping
            or any(not isinstance(nanit, str) or not nanit.strip()
                   or not isinstance(huckleberry, str) or not huckleberry.strip()
                   for nanit, huckleberry in mapping.items())):
        raise UidSelectionRequired("CHILD_UID_MAP must map nonempty Nanit UIDs to Huckleberry UIDs")
    if len(set(mapping.values())) != len(mapping):
        raise UidSelectionRequired("Each Huckleberry UID in CHILD_UID_MAP must be used once")
    return list(mapping.items())


async def nanit_uid_from_account(session):
    babies = await restore_nanit(session).async_get_babies()
    return select_uid([(baby.name, baby.uid) for baby in babies], "Nanit")


async def huckleberry_uid_from_account(api):
    user = await api.get_user()
    if user is None:
        raise UidSelectionRequired("Huckleberry user profile was not found")
    return select_uid([(child.nickname or "(unnamed)", child.cid)
                       for child in user.childList], "Huckleberry")


async def validate_uid_pairs(nanit, api, pairs):
    """Reject mapped UIDs that are not in the authenticated accounts."""
    babies = await nanit.async_get_babies()
    user = await api.get_user()
    if user is None:
        raise UidSelectionRequired("Huckleberry user profile was not found")
    nanit_uids = {baby.uid for baby in babies}
    huckleberry_uids = {child.cid for child in user.childList}
    for nanit_uid, huckleberry_uid in pairs:
        if nanit_uid not in nanit_uids:
            raise UidSelectionRequired(f"Nanit UID {nanit_uid} in CHILD_UID_MAP is not in this account")
        if huckleberry_uid not in huckleberry_uids:
            raise UidSelectionRequired(
                f"Huckleberry UID {huckleberry_uid} in CHILD_UID_MAP is not in this account")


async def log_missing_uids():
    """Show setup choices in container logs as soon as the service starts."""
    try:
        pairs = configured_uid_pairs()
    except UidSelectionRequired as exc:
        LOG.warning("UID configuration: %s", exc)
        return
    if pairs is not None:
        return
    async with aiohttp.ClientSession() as session:
        try:
            await asyncio.wait_for(nanit_uid_from_account(session), timeout=30)
        except Exception as exc:
            LOG.warning("Nanit UID discovery: %s", discovery_message(exc))
        try:
            api = HuckleberryAPI(email=os.environ["HUCKLEBERRY_EMAIL"],
                                 password=os.environ["HUCKLEBERRY_PASSWORD"],
                                 timezone=setting("TZ", "America/New_York"),
                                 websession=session)
            await asyncio.wait_for(authenticate_huckleberry(api), timeout=30)
            await asyncio.wait_for(huckleberry_uid_from_account(api), timeout=30)
        except Exception as exc:
            LOG.warning("Huckleberry UID discovery: %s", discovery_message(exc))


def discovery_message(exc):
    if isinstance(exc, KeyError):
        return f"Set {exc.args[0]} to look up children"
    if isinstance(exc, NanitReauthRequired):
        return "Nanit login is required before baby UIDs can be listed"
    if isinstance(exc, UidSelectionRequired):
        return str(exc)
    return f"lookup failed ({type(exc).__name__}); retry when the account is available"


def restore_nanit(session):
    if not STATE.exists():
        raise NanitReauthRequired("Nanit credentials have not been initialized")
    try:
        tokens = json.loads(STATE.read_text())
        client = NanitClient(session)
        client.restore_tokens(tokens["access_token"], tokens["refresh_token"])
    except (OSError, ValueError, KeyError) as exc:
        raise NanitReauthRequired("Nanit token file is unreadable or incomplete") from exc
    client.token_manager.on_tokens_refreshed(
        lambda access, refresh: save_tokens({"access_token": access, "refresh_token": refresh}))
    return client


async def calendar_sleep(client, baby_uid, start, end):
    """Only the unsupported calendar endpoint stays local to this project."""
    manager = client.token_manager
    params = {"start": int(start.timestamp()), "end": int(end.timestamp())}
    url = f"{API}/babies/{baby_uid}/calendar"
    headers = {
        "User-Agent": "Nanit/6.0.0 (iOS; iPhone; Scale/2.00)",
        "nanit-api-version": "1",
        "X-Nanit-Platform": "unknown",
        "X-Nanit-Service": "3.52.0 (882)",
    }
    for attempt in range(2):
        try:
            token = await manager.async_get_access_token()
        except NanitAuthError as exc:
            raise NanitReauthRequired("Nanit refresh token rejected") from exc
        try:
            async with client.session.get(url, params=params,
                                          headers={**headers, "Authorization": f"token {token}"},
                                          timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 401:
                    if attempt:
                        raise NanitReauthRequired("Nanit rejected a freshly refreshed access token")
                    try:
                        await manager.async_force_refresh(failed_token=token)
                    except NanitAuthError as exc:
                        raise NanitReauthRequired("Nanit refresh token rejected") from exc
                    continue
                if response.status != 200:
                    raise RuntimeError(f"Nanit calendar returned HTTP {response.status}")
                body = await response.json()
                if not isinstance(body, dict) or not isinstance(body.get("calendar"), list):
                    raise RuntimeError("Nanit calendar response has an unexpected shape")
                return body["calendar"]
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise NanitConnectionError(f"Nanit calendar unavailable: {type(exc).__name__}") from exc
    raise AssertionError("unreachable")


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


async def strict_sleep_intervals(api, child_uid, start, end):
    """Read all overlapping Huckleberry history, propagating Firestore errors."""
    from google.cloud import firestore
    from huckleberry_api.firebase_types import FirebaseSleepIntervalData, FirebaseSleepMultiContainer

    client = await api._get_firestore_client()
    collection = client.collection("sleep").document(child_uid).collection("intervals")
    start_ts, end_ts = start.timestamp(), end.timestamp()
    results = []
    regular = collection.where(filter=firestore.FieldFilter("start", "<", end_ts)).stream()
    async for doc in regular:
        data = doc.to_dict()
        if data and not data.get("multi"):
            interval = FirebaseSleepIntervalData.model_validate(data)
            if float(interval.start) + float(interval.duration) > start_ts:
                results.append(interval)
    multi = collection.where(filter=firestore.FieldFilter("multi", "==", True)).stream()
    async for doc in multi:
        data = doc.to_dict()
        if data:
            container = FirebaseSleepMultiContainer.model_validate(data)
            results.extend(entry for entry in container.data.values()
                           if float(entry.start) < end_ts
                           and float(entry.start) + float(entry.duration) > start_ts)
    return results


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


async def backfill(latest_day, days):
    dates = backfill_dates(latest_day, days)
    async with aiohttp.ClientSession() as websession:
        context = {"session": websession}
        for day in dates:
            LOG.info("Backfill: processing night starting %s", day)
            await sync_day(day, context)


async def sync_day(day, context=None):
    """Serialize local runs to prevent a scheduled run racing a manual backfill."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with (STATE.parent / "sync.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another sync is running against this data directory") from exc
        status_update("running", "sync_in_progress", last_attempt=utc_now(), night=day.isoformat())
        try:
            processed = await _sync_day(day, context)
        except Exception as exc:
            reason, detail = failure_reason(exc)
            status_update("error", reason, detail)
            raise
        if not processed:
            status_update("waiting", "window_not_started", "Waiting for the requested sleep period to begin")
            return False
        status_update("ok", "sync_succeeded", last_success=utc_now())
        return True


async def _sync_day(day, context=None):
    if context is None:
        async with aiohttp.ClientSession() as websession:
            return await _sync_day(day, {"session": websession})
    if "clients" not in context:
        context["clients"] = await prepare_clients(context["session"])
    nanit, api, pairs = context["clients"]
    return await _sync_day_with_clients(day, nanit, api, pairs)


async def prepare_clients(websession):
    pairs = configured_uid_pairs()
    mapped = pairs is not None
    if pairs is None:
        nanit_uid = await nanit_uid_from_account(websession)
    api = HuckleberryAPI(email=os.environ["HUCKLEBERRY_EMAIL"],
                         password=os.environ["HUCKLEBERRY_PASSWORD"],
                         timezone=setting("TZ", "America/New_York"), websession=websession)
    await authenticate_huckleberry(api)
    if pairs is None:
        huckleberry_uid = await huckleberry_uid_from_account(api)
        pairs = [(nanit_uid, huckleberry_uid)]
    nanit = restore_nanit(websession)
    if mapped:
        await validate_uid_pairs(nanit, api, pairs)
    return nanit, api, pairs


async def _sync_day_with_clients(day, nanit, api, pairs, now=None):
    tz = ZoneInfo(setting("TZ", "America/New_York"))
    gap = int(setting("MAX_WAKE_MINUTES", "20"))
    if not 0 <= gap <= 180:
        raise ValueError("MAX_WAKE_MINUTES must be between 0 and 180")
    write = boolean("WRITE_ENABLED")
    include_day = boolean("SYNC_DAYTIME")
    all_ready = True
    for nanit_uid, huckleberry_uid in pairs:
        LOG.info("Syncing Nanit %s to Huckleberry %s", nanit_uid, huckleberry_uid)
        if boolean("USE_HUCKLEBERRY_HOURS", True):
            child = await api.get_child(huckleberry_uid)
            if child is None or child.nightStart is None or child.morningCutoff is None:
                raise RuntimeError("Huckleberry nightStart/morningCutoff unavailable; configure explicit hours")
            night_start = profile_clock(child.nightStart, evening=True)
            morning_cutoff = profile_clock(child.morningCutoff)
        else:
            night_start = parse_clock(setting("NIGHT_START", "18:00"))
            morning_cutoff = parse_clock(setting("MORNING_CUTOFF", "10:00"))
        ranges = windows(day, tz, night_start, morning_cutoff, include_day)
        evening = ranges[-1][1]
        morning = ranges[-1][2]
        next_evening = local_boundary(day + timedelta(days=1), night_start, tz)
        now_ts = (now or datetime.now(timezone.utc)).timestamp()
        # Include daytime before this evening to preserve sleep crossing night start.
        day_start = local_boundary(day, morning_cutoff, tz)
        if day_start.timestamp() >= now_ts:
            all_ready = False
            continue
        # Include a pre-cutoff segment that may join a daytime continuation.
        fetch_start = datetime.fromtimestamp(day_start.timestamp() - gap * 60, tz)
        # Look past the next evening far enough to discover a short-wake continuation.
        fetch_end = datetime.fromtimestamp(
            min(next_evening.timestamp() + gap * 60, now_ts), tz)
        calendar = await calendar_sleep(nanit, nanit_uid, fetch_start, fetch_end)
        completed = completed_calendar_entries(calendar, now_ts)
        active_starts = [float(entry["begin_ts"]) for entry in calendar
                         if isinstance(entry, dict) and entry.get("type") == "auto_sleep"
                         and entry.get("end_ts") is None]
        # This strict read propagates failures. The library's list_sleep_intervals
        # catches some Firestore errors and otherwise returns an unsafe empty list.
        existing = [existing_range(x) for x in await strict_sleep_intervals(
            api, huckleberry_uid, fetch_start, fetch_end)]
        # Keep actual boundaries so a cross-cutoff sleep cannot become a daytime nap.
        spans = normalize(completed, fetch_start, fetch_end, gap * 60, clip=False)
        ready_end_ts = now_ts - gap * 60
        spans = [span for span in spans
                 if span[1].timestamp() <= ready_end_ts
                 and not any(span[0].timestamp() <= begin <= span[1].timestamp() + gap * 60
                             for begin in active_starts)]
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
        for kind, spans in spans_by_kind:
            LOG.info("%s %s: %d Nanit interval(s)", day, kind, len(spans))
            for span in spans:
                if any(overlap(span, old) for old in existing):
                    LOG.warning("Skipping %s–%s: overlaps existing Huckleberry sleep", *span)
                    continue
                LOG.info("%s %s–%s (%s)", "WRITE" if write else "DRY RUN", *span, kind)
                if write:
                    # Re-read immediately before writing in case the app changed mid-run.
                    current = [existing_range(x) for x in await strict_sleep_intervals(
                        api, huckleberry_uid, span[0], span[1])]
                    if any(overlap(span, old) for old in current):
                        LOG.warning("Skipping %s–%s: Huckleberry changed during sync", *span)
                        continue
                    await api.log_sleep(huckleberry_uid, start_time=span[0], end_time=span[1])
                    existing.append(span)
    return all_ready


async def scheduled():
    tz = ZoneInfo(setting("TZ", "America/New_York"))
    async with aiohttp.ClientSession() as websession:
        context = {"session": websession}
        while True:
            now_ts = datetime.now(timezone.utc).timestamp()
            next_tick = (int(now_ts) // (15 * 60) + 1) * (15 * 60)
            LOG.info("Next sync check at %s", datetime.fromtimestamp(next_tick, tz))
            await asyncio.sleep(max(1, next_tick - now_ts))
            try:
                today = datetime.now(tz).date()
                # Cover the night across midnight and today's daytime/evening sleep.
                for day in (today - timedelta(days=1), today):
                    await sync_day(day, context)
            except Exception:
                LOG.exception("Scheduled sync failed; will retry in 15 minutes")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("login", "once", "backfill", "serve", "babies", "children", "status", "healthcheck"))
    parser.add_argument("--date", type=date.fromisoformat, help="Latest evening date, YYYY-MM-DD (once/backfill)")
    parser.add_argument("--days", type=int, help="Number of nights (backfill; otherwise BACKFILL_DAYS)")
    args = parser.parse_args()
    if args.command == "login":
        asyncio.run(nanit_login())
    elif args.command == "babies":
        async def show_babies():
            async with aiohttp.ClientSession() as session:
                for baby in await restore_nanit(session).async_get_babies():
                    print(baby.name, baby.uid)
        asyncio.run(show_babies())
    elif args.command == "children":
        asyncio.run(huckleberry_children())
    elif args.command == "status":
        healthy, message = health_status()
        print(json.dumps({"healthy": healthy, "message": message,
                          "status": json.loads(STATUS.read_text()) if STATUS.exists() else None}, indent=2))
    elif args.command == "healthcheck":
        healthy, message = health_status()
        print(message)
        if not healthy:
            sys.exit(1)
    elif args.command == "once":
        tz = ZoneInfo(setting("TZ", "America/New_York"))
        asyncio.run(sync_day(args.date or datetime.now(tz).date() - timedelta(days=1)))
    elif args.command == "backfill":
        tz = ZoneInfo(setting("TZ", "America/New_York"))
        latest = args.date or datetime.now(tz).date() - timedelta(days=1)
        days = args.days if args.days is not None else int(setting("BACKFILL_DAYS", "7"))
        asyncio.run(backfill(latest, days))
    else:
        asyncio.run(log_missing_uids())
        try:
            prior = json.loads(STATUS.read_text()) if STATUS.exists() else {}
        except (OSError, ValueError):
            prior = {}
        if not isinstance(prior, dict) or prior.get("state") != "error":
            status_update("waiting", "scheduler_started", service_started=utc_now())
        try:
            asyncio.run(scheduled())
        except Exception as exc:
            reason, detail = failure_reason(exc)
            status_update("error", reason, detail)
            raise


if __name__ == "__main__":
    main()
