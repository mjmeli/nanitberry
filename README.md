# Nanit → Huckleberry sleep sync

See [HANDOFF.md](HANDOFF.md) for the original requirements, decisions, current validation limits, and recommended next steps for continuing development.

A scheduled Docker container that imports completed Nanit `auto_sleep` calendar intervals into Huckleberry. Nighttime wake gaps at or below `MAX_NIGHT_WAKE_MINUTES` are bridged; larger gaps remain excluded. Daytime sync is disabled by default. This is an independent project: it does not need Home Assistant and does not modify your existing integrations.

Nanit authentication, MFA, baby lookup, and token refresh use the pinned `aionanit` library from the `ha-nanit` project. One small direct REST request reads `/babies/{uid}/calendar`, which `aionanit` 1.12.2 does not expose. This request uses the library's managed token and preserves the previous token file format. A [September 2026 comment on ha-nanit issue #49](https://github.com/wealthystudent/ha-nanit/issues/49) reports that this calendar response contains camera detected `auto_sleep` and manually logged `sleep` entries. The importer selects only `auto_sleep`; it does not import manually logged Nanit naps. The comment is useful independent evidence, but account-specific behavior still needs a credentialed dry run. When a suitable library method ships, compare its data with this calendar response before replacing `calendar_sleep`.

## Setup

1. Copy `.env.example` to `.env`. Set a persistent NAS-backed `DATA_DIR`, account credentials, time zone, baby IDs, and schedule. Do not commit `.env` or share the token file.
2. From this folder, run `docker compose build` and `docker compose run --rm -it sync python sync.py login`. Nanit will send a one-time MFA code. Enter it in the terminal. The access and refresh tokens are saved to `/data/nanit_tokens.json`, with restrictive file permissions. Regular runs refresh the session without another code. If Nanit revokes the refresh token, repeat the interactive login; the sync never attempts to bypass MFA.
3. To find the Nanit UID, run `docker compose run --rm sync python sync.py babies`. The Huckleberry child UID is the `cid` from Huckleberry's account data (or its HA integration's child/device data). Do not guess IDs: both must refer to the same child.
4. With `WRITE_ENABLED=false`, run `docker compose run --rm sync python sync.py once --date YYYY-MM-DD` (the date the night *started*). Review the proposed intervals in the logs against Nanit and Huckleberry. `docker compose run --rm sync python sync.py once` uses last night's date automatically.
5. To review historical nights, run `docker compose run --rm sync python sync.py backfill`. This processes `BACKFILL_DAYS` completed nights, oldest first. Override the count with `--days 14`, or set the latest evening date with `--date YYYY-MM-DD`. Review the proposed intervals; with daytime sync enabled, the command also reviews the corresponding prior days' naps.
6. Change `WRITE_ENABLED=true` only after reviewing representative data, then run `docker compose run --rm sync python sync.py backfill` to import the selected history. Start the daily service with `docker compose up -d`. It subsequently processes just the previous night at `RUN_AT`. Re-running backfill checks Huckleberry for existing intervals and skips overlaps.

For Portainer, deploy the Compose file with its environment variables and bind the NAS directory to `/data`. Portainer's web console may not offer a suitable interactive MFA terminal; run the one-time `login` command from the Docker host or a terminal attached to the container. Build the image where Portainer can access the project folder, or publish it to your preferred registry and substitute `image:` for `build:`.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATA_DIR` | required | Persistent host path for Nanit tokens |
| `TZ` | `America/New_York` | Local schedule and sleep boundary time zone |
| `RUN_AT` | `11:00` | Daily local clock time; use after the morning cutoff and Nanit's analysis delay |
| `HEALTH_MAX_AGE_HOURS` | `36` | Docker becomes unhealthy if no successful run within this interval |
| `BACKFILL_DAYS` | `7` | Number of completed nights imported by the explicit `backfill` command (1–365); no automatic backfill at startup |
| `NIGHT_START` | `18:00` | Start of the night classification window |
| `MORNING_CUTOFF` | `10:00` | End of the night classification window on the following day |
| `USE_HUCKLEBERRY_HOURS` | `false` | Read `nightStart` and `morningCutoff` from the Huckleberry child profile if both are present as `HH:MM`; fail clearly for unsupported formats |
| `MAX_NIGHT_WAKE_MINUTES` | `20` | Bridge gaps up to this many minutes (0–180) |
| `SYNC_DAYTIME` | `false` | Import prior day's segments from morning cutoff through night start |
| `MAX_DAY_WAKE_MINUTES` | `0` | Gap bridging for optional daytime sync (0–180) |
| `WRITE_ENABLED` | `false` | Set `true` to write; otherwise log proposed imports |

The cutoff hours classify data and clip intervals, so choose a window that includes the baby's actual bedtime and morning waking. Huckleberry's profile field format is not established for every account; keep `USE_HUCKLEBERRY_HOURS=false` until a dry run proves it works. A 10-minute feed bridged by the configured threshold appears as continuous logged sleep; a 45-minute wake remains a gap. This deliberately follows a practical logging convention and is not a measure of physiological minutes asleep.

The importer fails closed if the requested night is still in progress, Nanit returns malformed automatic sleep timestamps, or Huckleberry's history read fails. It skips any interval overlapping existing Huckleberry sleep and rechecks just before each write. A local file lock prevents a manual backfill and scheduled run sharing `/data` from racing; run only one container instance per account. It will not overwrite or split your manual entries. Ambiguous or nonexistent configured clock boundaries on a daylight-saving transition produce an error; choose an unambiguous boundary such as the default hours. If Nanit revises a night after import, the importer also will not silently replace it: review and correct that night manually. Nanit and Huckleberry are unofficial APIs and can change. Docker image builds and live API behavior need verification in your environment; no credentials or account access are included here.

## Health and expired authentication

Run `docker compose exec sync python sync.py status` for the latest attempt, last successful sync, and a reason when unhealthy. Docker's health check runs every five minutes; `docker inspect nanit-huckleberry-sync --format '{{json .State.Health}}'` shows its recent output. The same report is stored as `/data/sync_status.json`, without passwords or tokens. After a fresh install, the container waits for its first scheduled run; use `once` for an immediate dry run.

An expired **access token** is normal: `aionanit` refreshes it automatically and persists the rotated pair. A rejected **refresh token**, missing token file, or a second calendar authorization rejection is reported as `nanit_reauth_required`, with the interactive login command. A temporary refresh endpoint error is reported separately as `nanit_api_error`; it does not claim the MFA session expired. A rejected Huckleberry sign-in reports `huckleberry_auth_rejected`. The service is unhealthy until its first successful dry run or import. Any later failed run remains visible across a service restart until a successful run. If the scheduler misses a successful run for more than `HEALTH_MAX_AGE_HOURS`, it reports `sync_stale`.

## Security notes

The container makes outbound HTTPS requests to Nanit and Huckleberry/Firebase and exposes no listening port. TLS verification uses Python's standard HTTPS clients. Compose drops Linux capabilities, blocks privilege escalation, and mounts the container root filesystem read-only; only `/data` is writable. Huckleberry API 0.4.7 and aionanit 1.12.2 are pinned, though their transitive Python dependencies and the `python:3.14-slim` base tag are not digest-locked.

Treat `.env` and `/data/nanit_tokens.json` as credentials. `.env` contains both account passwords and should be readable only by the Docker operator; the token file and status file are created with mode 0600. Docker administrators can still inspect container environment variables, and anyone who can read the mounted data can use the refresh token. Avoid publishing or backing up these values unencrypted. This is a small personal-use project using unofficial APIs, not a formal security-audited service.

## Follow-up TODOs after credentials are available

- [ ] Inspect a Nanit calendar response containing a manually logged nap and confirm the reported `type: "sleep"`, timestamps, and any edits or corrections on this account. Compare it with an automatic nap and the Nanit app display; avoid recording credentials or identifying data in fixtures.
- [ ] Decide whether manual Nanit naps should be eligible when `SYNC_DAYTIME=true`, while preserving Huckleberry manual naps and the existing overlap check. Add a fixture and test for the confirmed event shape before enabling imports.
- [ ] Test the one-time MFA login, token refresh, and a full dry run against one completed night. Confirm whether Huckleberry's `nightStart` and `morningCutoff` values use the `HH:MM` format on this account.
- [ ] Verify a historical backfill dry run against existing Huckleberry entries before enabling writes.
