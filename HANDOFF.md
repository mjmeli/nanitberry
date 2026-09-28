# Codex handoff: Nanit → Huckleberry sleep sync

## Goal and user context

Build and validate a small Docker service that transfers Nanit sleep data into Huckleberry. The user prefers tracking daytime naps manually in Huckleberry because that is more accurate for them. Overnight, they want Nanit's observed sleep intervals represented accurately enough that Huckleberry data is useful.

The motivating example is a reported 8 p.m.–7 a.m. night. Brief wakeups for feeds should generally be treated as part of the night sleep span; a longer wake, such as 45 minutes spent awake and having difficulty falling back asleep, should split the sleep so the awake time is excluded. The existing Huckleberry entry is currently one inaccurate, uninterrupted block. Keep the gap-bridging rule configurable because the right threshold is a household preference and must be validated against real Nanit data.

The default product behavior should be night-only. Daytime imports must remain optional (`SYNC_DAYTIME=false` by default) because the user prefers manual daytime tracking. If enabled, daytime gap behavior is separately configurable.

## Agreed design

- Run as a Docker container, configurable by environment variables and Compose. The user has used Portainer and Synology NAS storage; persist tokens/status on a mounted NAS path. They have an existing Home Assistant integration for both Nanit and Huckleberry, but prefer this independent container for the first implementation.
- Use a hybrid Nanit client: `aionanit` from the `wealthystudent/ha-nanit` project for Nanit login, MFA, baby lookup, and token lifecycle; retain a small custom REST request for sleep calendar data until the library exposes a suitable method.
- Keep the calendar call isolated until aionanit exposes a confirmed equivalent. A September 2026 comment on issue #49 now reports `/babies/{uid}/calendar` with both `auto_sleep` and manual `sleep` entries, matching Blair's endpoint. This does not establish account-specific behavior or promise an aionanit calendar method.
- Nanit MFA is interactive for initial login. Store access/refresh tokens in the mounted data directory with restrictive permissions, and support token refresh for scheduled runs. If the refresh token expires/revokes, health status should tell the operator to repeat login and distinguish this from temporary API/network failures.
- Make scheduled run time, sleep-window boundaries, wake-gap thresholds, daytime sync, write enablement, and backfill count configurable.
- Backfill should be explicit and configurable; no automatic historical write on container startup. Dry-run by default.
- Avoid duplicate Huckleberry intervals. Read existing intervals, skip overlaps, fail closed when history reads fail, and recheck just before writes. Do not overwrite or split manual Huckleberry entries.
- TODO after credentials: determine how manually entered Nanit naps appear in API responses; do not include manual naps in sync until event shape and intended policy are verified.

## Current project state

The project is in this folder. `sync.py` is Python, the image is based on `python:3.14-slim`, and the Dockerfile pins `aionanit==1.12.2` and `huckleberry-api==0.4.7`.

Implemented commands include `login`, `babies`, `once`, `backfill`, `serve`, `status`, and `healthcheck`. Writes are disabled by default. The current Nanit calendar call fetches `auto_sleep` entries, clips to the configured windows, merges intervals separated by no more than the configured gap, and sends resulting spans to Huckleberry. Daytime sync is optional. Tokens/status live under `/data`; Compose mounts a host path there. Security controls include a read-only root filesystem, dropped capabilities, `no-new-privileges`, HTTPS clients, and restrictive token file permissions.

The Nanit auth integration uses the public `NanitClient` API and token refresh callback. The custom calendar request obtains a managed token from aionanit and retries once after HTTP 401. Existing token JSON shape remains `{ "access_token": ..., "refresh_token": ... }`.

## Important validation status and limitations

- Twelve local unit tests pass: interval merging/gap handling, manual nap exclusion, malformed sleep rejection, DST overlap and clock boundaries, fractional interval overlap, date/backfill ordering, Firestore read failure behavior, initial and last-second duplicate blocking, health reporting, private token file rotation and refresh callback persistence, and calendar 401 token retry.
- No live Nanit or Huckleberry credentials have been supplied in this work, and no account/API dry run has been performed. Docker is not available in the current workspace, so the image has not been built here.
- Before enabling writes, perform an interactive Nanit MFA login, identify both baby/child IDs, run multiple completed nights with `WRITE_ENABLED=false`, compare proposed intervals with the Nanit app and Huckleberry, test auth refresh, and verify Huckleberry interval reads/writes.
- Recheck the exact Nanit sleep response schema on the account. Blair (`mjmeli/blair`) uses `/babies/{uid}/calendar` and `auto_sleep` entries; issue #49 now has an independent report of both `auto_sleep` and manual `sleep` in that response. Do not conflate calendar intervals with every event shown in Nanit's timeline.
- Test whether Huckleberry exposes `nightStart` and `morningCutoff` in the assumed `HH:MM` form before setting `USE_HUCKLEBERRY_HOURS=true`; explicit boundaries are the default.
- Current policy bridges short awake gaps into continuous logged sleep. This is a practical logging convention, not a claim that Nanit recorded the baby asleep for every minute inside the merged span.
- Repeated imports are overlap-safe but intentionally do not update/delete an already imported interval if Nanit later revises that night. Corrections currently require manual review.

## Suggested next steps for Codex

1. Review `sync.py`, `README.md`, `compose.yaml`, `.env.example`, and `tests/` before changing behavior.
2. Inspect the current `aionanit` release API and issue #49 before deciding whether a library calendar method now exists. If it does, compare its semantics/schema with the current calendar request before replacing it.
3. Review correctness details around timezone/DST boundaries, sleep segment types, overlapping/adjacent intervals, scheduler failure visibility, token refresh persistence, and Huckleberry's duplicate matching.
4. Improve tests where a concrete uncertainty is found; preserve dry-run and no-duplicate protections.
5. Once credentials are available, validate a small backfill dry run and manual-nap event shape before enabling any production writes.

## Relevant references

- User's example Nanit client project: <https://github.com/mjmeli/blair>
- Nanit Home Assistant integration and aionanit package: <https://github.com/wealthystudent/ha-nanit>
- Related sleep/activity investigation: <https://github.com/wealthystudent/ha-nanit/issues/49>
- Huckleberry Python API wrapper: <https://github.com/Woyken/huckleberry-api>

Never put Nanit/Huckleberry passwords, MFA codes, refresh tokens, or identifying API fixtures in this handoff or in source control. Use a local `.env` and mounted state directory.
