"""Private atomic JSON persistence and service health reports."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config


def utc_now():
    return datetime.now(ZoneInfo("UTC")).isoformat()


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


def status_update(state, reason, detail="", *, paths=None, **extra):
    paths = paths or config.DEFAULT_PATHS
    paths.status.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(paths.status.read_text()) if paths.status.exists() else {}
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    report = {**previous, "state": state, "reason": reason, "detail": detail,
              "last_update": utc_now(), **extra}
    private_json(paths.status, report, indent=2)


def health_status(*, paths=None, max_age_hours=None):
    paths = paths or config.DEFAULT_PATHS
    if not paths.tokens.exists():
        return False, "nanit_reauth_required: run interactive login"
    try:
        tokens = json.loads(paths.tokens.read_text())
        if not isinstance(tokens, dict) or not all(
            isinstance(tokens.get(key), str) and tokens[key]
            for key in ("access_token", "refresh_token")
        ):
            raise ValueError("incomplete token file")
    except (OSError, ValueError):
        return False, "nanit_reauth_required: token file is unreadable or incomplete"
    try:
        report = json.loads(paths.status.read_text())
    except (OSError, ValueError):
        return False, "no_sync_status: run a dry run or wait for the first scheduled run"
    if not isinstance(report, dict):
        return False, "invalid_sync_status"
    if report.get("state") == "error":
        return False, f"{report.get('reason')}: {report.get('detail', '')}"
    max_age = int(config.setting("HEALTH_MAX_AGE_HOURS", "36") if max_age_hours is None else max_age_hours)
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


def save_tokens(data, *, paths=None):
    paths = paths or config.DEFAULT_PATHS
    tokens = {key: data.get(key) for key in ("access_token", "refresh_token")}
    if not all(tokens.values()):
        raise RuntimeError("Nanit did not return access and refresh tokens")
    private_json(paths.tokens, tokens)


def huckleberry_token_path(*, paths=None):
    return (paths or config.DEFAULT_PATHS).huckleberry_tokens


def save_huckleberry_token(api, *, paths=None):
    if not api.refresh_token or not api.user_uid:
        raise RuntimeError("Huckleberry did not return a refresh token and user UID")
    private_json(huckleberry_token_path(paths=paths), {
        "email": api.email,
        "refresh_token": api.refresh_token,
        "user_uid": api.user_uid,
    })

