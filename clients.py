"""Authentication, child selection, and Nanit calendar access."""
import asyncio
import json
import logging
import sys

import aiohttp
from aionanit import NanitAuthError, NanitClient, NanitConnectionError, NanitMfaRequiredError
from huckleberry_api import HuckleberryAPI

import config
import storage
import errors

LOG = logging.getLogger("nanit_huckleberry_sync")
API = "https://api.nanit.com"


async def authenticate_huckleberry(api, *, paths=None):
    """Reuse a saved Firebase refresh token across short-lived containers."""
    refresh = getattr(api, "refresh_session_token", None)
    if refresh is None:
        # Synthetic API objects used by local tests do not manage tokens.
        await api.authenticate()
        return

    async def refresh_and_save():
        await refresh()
        storage.save_huckleberry_token(api, paths=paths)

    api.refresh_session_token = refresh_and_save
    try:
        cached = json.loads(storage.huckleberry_token_path(paths=paths).read_text())
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
    storage.save_huckleberry_token(api, paths=paths)


async def nanit_login(*, settings=None, paths=None):
    settings = settings or config.Settings.from_env()
    email, password = settings.nanit_email, settings.nanit_password
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
    storage.save_tokens(result, paths=paths)
    storage.status_update("waiting", "login_complete", "Waiting for first sync", service_started=storage.utc_now(), paths=paths)
    LOG.info("Nanit login succeeded; refresh credentials stored at %s", (paths or config.DEFAULT_PATHS).tokens)


async def huckleberry_children(*, settings=None, paths=None):
    """List the child IDs available to the configured Huckleberry account."""
    settings = settings or config.Settings.from_env()
    async with aiohttp.ClientSession() as session:
        api = HuckleberryAPI(
            **settings.huckleberry_credentials(),
            timezone=settings.timezone,
            websession=session,
        )
        await authenticate_huckleberry(api, paths=paths)
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
            raise errors.UidSelectionRequired(f"The only {account} child has no usable UID")
        LOG.info("Using the only %s child", account)
        return items[0][1]
    if not items:
        raise errors.UidSelectionRequired(f"No {account} children found; check account access")
    raise errors.UidSelectionRequired(
        f"Multiple {account} children found; set CHILD_UID_MAP with matching UIDs shown above")


def configured_uid_pairs(settings=None):
    """Return explicit Nanit-to-Huckleberry pairs, if configured."""
    raw = (settings or config.Settings.from_env()).child_uid_map.strip()
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
        raise errors.UidSelectionRequired("CHILD_UID_MAP must be a JSON object with unique Nanit UIDs") from exc
    if (not isinstance(mapping, dict) or not mapping
            or any(not isinstance(nanit, str) or not nanit.strip()
                   or not isinstance(huckleberry, str) or not huckleberry.strip()
                   for nanit, huckleberry in mapping.items())):
        raise errors.UidSelectionRequired("CHILD_UID_MAP must map nonempty Nanit UIDs to Huckleberry UIDs")
    if len(set(mapping.values())) != len(mapping):
        raise errors.UidSelectionRequired("Each Huckleberry UID in CHILD_UID_MAP must be used once")
    return list(mapping.items())


async def nanit_uid_from_account(session, *, paths=None):
    babies = await restore_nanit(session, paths=paths).async_get_babies()
    return select_uid([(baby.name, baby.uid) for baby in babies], "Nanit")


async def huckleberry_uid_from_account(api):
    user = await api.get_user()
    if user is None:
        raise errors.UidSelectionRequired("Huckleberry user profile was not found")
    return select_uid([(child.nickname or "(unnamed)", child.cid)
                       for child in user.childList], "Huckleberry")


async def validate_uid_pairs(nanit, api, pairs):
    """Reject mapped UIDs that are not in the authenticated accounts."""
    babies = await nanit.async_get_babies()
    user = await api.get_user()
    if user is None:
        raise errors.UidSelectionRequired("Huckleberry user profile was not found")
    nanit_uids = {baby.uid for baby in babies}
    huckleberry_uids = {child.cid for child in user.childList}
    for nanit_uid, huckleberry_uid in pairs:
        if nanit_uid not in nanit_uids:
            raise errors.UidSelectionRequired(f"Nanit UID {nanit_uid} in CHILD_UID_MAP is not in this account")
        if huckleberry_uid not in huckleberry_uids:
            raise errors.UidSelectionRequired(
                f"Huckleberry UID {huckleberry_uid} in CHILD_UID_MAP is not in this account")


async def log_missing_uids(*, settings=None, paths=None):
    """Show setup choices in container logs as soon as the service starts."""
    settings = settings or config.Settings.from_env()
    try:
        pairs = configured_uid_pairs(settings)
    except errors.UidSelectionRequired as exc:
        LOG.warning("UID configuration: %s", exc)
        return
    if pairs is not None:
        return
    async with aiohttp.ClientSession() as session:
        try:
            await asyncio.wait_for(nanit_uid_from_account(session, paths=paths), timeout=30)
        except Exception as exc:
            LOG.warning("Nanit UID discovery: %s", discovery_message(exc))
        try:
            api = HuckleberryAPI(**settings.huckleberry_credentials(),
                                 timezone=settings.timezone,
                                 websession=session)
            await asyncio.wait_for(authenticate_huckleberry(api, paths=paths), timeout=30)
            await asyncio.wait_for(huckleberry_uid_from_account(api), timeout=30)
        except Exception as exc:
            LOG.warning("Huckleberry UID discovery: %s", discovery_message(exc))


def discovery_message(exc):
    if isinstance(exc, KeyError):
        return f"Set {exc.args[0]} to look up children"
    if isinstance(exc, errors.NanitReauthRequired):
        return "Nanit login is required before baby UIDs can be listed"
    if isinstance(exc, errors.UidSelectionRequired):
        return str(exc)
    return f"lookup failed ({type(exc).__name__}); retry when the account is available"


def restore_nanit(session, *, paths=None):
    paths = paths or config.DEFAULT_PATHS
    if not paths.tokens.exists():
        raise errors.NanitReauthRequired("Nanit credentials have not been initialized")
    try:
        tokens = json.loads(paths.tokens.read_text())
        client = NanitClient(session)
        client.restore_tokens(tokens["access_token"], tokens["refresh_token"])
    except (OSError, ValueError, KeyError) as exc:
        raise errors.NanitReauthRequired("Nanit token file is unreadable or incomplete") from exc
    client.token_manager.on_tokens_refreshed(
        lambda access, refresh: storage.save_tokens({"access_token": access, "refresh_token": refresh}, paths=paths))
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
            raise errors.NanitReauthRequired("Nanit refresh token rejected") from exc
        try:
            async with client.session.get(url, params=params,
                                          headers={**headers, "Authorization": f"token {token}"},
                                          timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status == 401:
                    if attempt:
                        raise errors.NanitReauthRequired("Nanit rejected a freshly refreshed access token")
                    try:
                        await manager.async_force_refresh(failed_token=token)
                    except NanitAuthError as exc:
                        raise errors.NanitReauthRequired("Nanit refresh token rejected") from exc
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


async def prepare_clients(websession, *, settings=None, paths=None):
    settings = settings or config.Settings.from_env()
    pairs = configured_uid_pairs(settings)
    mapped = pairs is not None
    if pairs is None:
        nanit_uid = await nanit_uid_from_account(websession, paths=paths)
    api = HuckleberryAPI(**settings.huckleberry_credentials(),
                         timezone=settings.timezone, websession=websession)
    await authenticate_huckleberry(api, paths=paths)
    if pairs is None:
        huckleberry_uid = await huckleberry_uid_from_account(api)
        pairs = [(nanit_uid, huckleberry_uid)]
    nanit = restore_nanit(websession, paths=paths)
    if mapped:
        await validate_uid_pairs(nanit, api, pairs)
    return nanit, api, pairs

