# nanitberry

Sync completed Nanit sleep into Huckleberry from a small Docker service. Short overnight wake gaps can be bridged; longer wakes produce separate Huckleberry sleep entries. The gap threshold is configurable.

The default is **night-only, dry-run**. Daytime sync is optional, and writes require `WRITE_ENABLED=true`. The importer reads Nanit calendar entries of type `auto_sleep`; manually logged Nanit naps are not imported.

## Quick start with Docker Compose

[compose.yaml](compose.yaml) is an example deployment. It pulls the published GHCR image and stores tokens and sync status in `./data`. Other Docker setups only need the same environment variables and a persistent, writable `/data` mount.

1. Copy `.env.example` to `.env`. Set both account credentials and `TZ`.
2. Pull the image and complete Nanit's interactive MFA login:

   ```sh
   docker compose pull
   docker compose run --rm -it nanitberry python sync.py login
   ```

3. Start the service with `CHILD_UID_MAP` blank (`docker compose up -d`), then view `docker compose logs nanitberry` or the container logs in Portainer. Startup logs list available Nanit babies and Huckleberry children with their UIDs. If each account has exactly one child, that child is selected automatically for each sync. To choose one child or sync several children, set `CHILD_UID_MAP` as described below and restart the service. You can also list the IDs with:

   ```sh
   docker compose run --rm nanitberry python sync.py babies
   docker compose run --rm nanitberry python sync.py children
   ```

   `babies` prints Nanit baby names and UIDs. `children` prints Huckleberry nicknames and `cid` values. The Huckleberry child UID is the `cid`. Nanit baby lookup requires the saved token pair from step 2; until then, startup logs explain that login is needed. Huckleberry children can still be listed independently. With `CHILD_UID_MAP` blank, the service lists available IDs on each restart.

4. Preview one completed night while writes are disabled:

   ```sh
   docker compose run --rm nanitberry python sync.py once --date YYYY-MM-DD
   ```

   The date is the evening the night began. Omit `--date` to use the previous evening. Compare the proposed intervals with both apps. To review history, preview `backfill --days 7` while writes are disabled.

5. Set `WRITE_ENABLED=true` only when the previews look right. Run `once` or an explicit `backfill` to import selected history, then restart the service with the updated configuration:

   ```sh
   docker compose run --rm nanitberry python sync.py backfill --days 7
   docker compose up -d
   ```

   Startup does not backfill. The service checks the previous night on every quarter hour, so a 7:15 a.m. cutoff is checked at 7:15 when the service is running. It begins importing completed sleep at the morning cutoff and keeps checking for a sleep that continues past it. A segment is held until its configured wake-gap period has elapsed, so a short wake can still join resumed sleep in one Huckleberry entry. With the default 20-minute gap, a newly ended segment is normally imported at the first 15-minute check at least 20 minutes later.

For plain Docker, use `ghcr.io/mjmeli/nanitberry:latest` with `--env-file .env` and a persistent mount at `/data`. The image's default command starts the scheduler. Run a command with `docker run --rm --env-file .env -v nanitberry-state:/data ghcr.io/mjmeli/nanitberry:latest python sync.py ...` (add `-it` for `login`).

## Images

The [image workflow](.github/workflows/build-and-publish-images.yml) tests every PR and builds its image without pushing. Branch pushes and `v*` tags publish to `ghcr.io/mjmeli/nanitberry` with branch, commit, and version tags; `latest` tracks the default branch or a version tag. If the GitHub Actions secret `DOCKERHUB_TOKEN` is configured, the same tags are also published to `mjmeli/nanitberry` on Docker Hub. The token must have permission to push to that Docker Hub repository.

The Compose example uses the published GHCR image. To test a local build, run `docker build -t nanitberry:local .` and add `-f compose.yaml -f compose.local.yaml` to your Compose commands. The image does not need to be in `.env`. A private GHCR package requires Docker authentication to pull; package visibility is configured on GitHub.

## Configuration

Required for sync: `HUCKLEBERRY_EMAIL`, `HUCKLEBERRY_PASSWORD`, and a saved Nanit token pair. The first Huckleberry login saves a refresh token in `/data/huckleberry_tokens.json`; later runs refresh it without another password login. `CHILD_UID_MAP` is the only UID setting. Leave it blank to select automatically when **both** accounts have exactly one child. Otherwise, set a JSON object mapping each Nanit UID to its matching Huckleberry UID. Use one entry to sync one selected child, or several entries to sync several children:

```dotenv
CHILD_UID_MAP={"nanit-uid-1":"huckleberry-uid-1","nanit-uid-2":"huckleberry-uid-2"}
```

Each Nanit and Huckleberry UID can appear only once in the map. Every scheduled run, preview, and backfill processes all mapped pairs in order. Preview all pairs before enabling writes; if a later pair fails after an earlier pair was written, rerunning is safe because existing Huckleberry intervals are checked again. `NANIT_EMAIL` and `NANIT_PASSWORD` are needed for interactive login; scheduled runs use the saved token pair. Keep `.env` private. Container logs contain child names and UIDs during discovery.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TZ` | `America/New_York` | IANA time zone for scheduling and sleep windows |
| `USE_HUCKLEBERRY_HOURS` | `true` | Read each child's night start and morning cutoff from its Huckleberry profile |
| `NIGHT_START` | `18:00` | Evening boundary only when `USE_HUCKLEBERRY_HOURS=false` |
| `MORNING_CUTOFF` | `10:00` | Morning boundary only when `USE_HUCKLEBERRY_HOURS=false` |
| `MAX_WAKE_MINUTES` | `20` | Bridge wake gaps up to this length in night and optional daytime sleep; `0` disables bridging |
| `SYNC_DAYTIME` | `false` | Also consider automatic sleep between morning cutoff and night start |
| `WRITE_ENABLED` | `false` | Enable Huckleberry writes |
| `BACKFILL_DAYS` | `7` | Number of completed nights for an explicit `backfill` (1–365) |
| `HEALTH_MAX_AGE_HOURS` | `36` | Mark sync unhealthy when the last success is too old |
| `STATE_DIR` | `/data` | Container directory for tokens, status, and the local lock |

## How night sleep is defined

By default, each child's Huckleberry profile supplies the night start and morning cutoff. Its values may be `HH:MM` or fractional hours: night start `8.0` means 8 p.m., and morning cutoff `7.25` means 7:15 a.m. With those settings, the night window for an evening date runs from **8:00 p.m. to 7:15 a.m. the next day**.

Nanitberry joins automatic Nanit sleep segments separated by at most `MAX_WAKE_MINUTES` (20 by default). A resulting interval belongs to that night if it **ends after 8:00 p.m. and begins before 7:15 a.m.** The boundaries decide which night owns the interval; they do not trim its start or end. For example:

| Nanit sleep | Night sleep? | Interval sent to Huckleberry |
| --- | --- | --- |
| 7:45 p.m.–7:30 a.m. | Yes; crosses both boundaries | 7:45 p.m.–7:30 a.m. |
| 7:45 p.m.–10:00 p.m. | Yes; starts before night start | 7:45 p.m.–10:00 p.m. |
| 11:00 p.m.–7:30 a.m. | Yes; ends after morning cutoff | 11:00 p.m.–7:30 a.m. |
| 11:00 p.m.–6:00 a.m. | Yes; entirely inside the window | 11:00 p.m.–6:00 a.m. |
| 7:00 p.m.–7:45 p.m. | No; ends before night start | None in night-only mode |
| 7:30 a.m.–8:00 a.m. | No; starts after morning cutoff | None in night-only mode |

Ending exactly at 8:00 p.m. does not overlap the night; starting exactly at 7:15 a.m. belongs to daytime. A post-cutoff segment *can* be part of night sleep when it follows a pre-cutoff segment within the wake-gap threshold. In that case the joined interval, including the wake minutes, is logged as one night entry. A longer gap leaves separate intervals, and a new post-cutoff interval is not night sleep. `SYNC_DAYTIME=true` also considers daytime intervals; one that crosses night start is assigned to night only once.

For each night, the Nanit calendar search starts at that evening date's morning cutoff and ends no later than the following evening. Sleep that began before the search start, or continues past the search end, is outside this run's coverage. An in-progress Nanit entry without an end time is held for a later check. Ambiguous or nonexistent local clock boundaries during a daylight-saving change fail with an error.

## Safety and status

Before writing, nanitberry reads Huckleberry sleep history and skips any proposed interval that overlaps an existing entry, including a manual one. It reads again immediately before each write. A failed history read stops the run. The service does not edit or delete existing sleep; later Nanit corrections require manual review. One container instance should use a state volume at a time.

Use `docker compose run --rm nanitberry python sync.py status` to inspect the latest attempt, or `docker compose ps` for Docker health. The health check stays unhealthy until the first successful run, reports failed runs, and becomes stale after `HEALTH_MAX_AGE_HOURS`. A rejected Nanit refresh token requires another interactive `login`; temporary Nanit failures are reported separately. Rotated Nanit and Huckleberry tokens are saved under `/data` with mode `0600`.

Nanit and Huckleberry are unofficial APIs and can change. Authentication, MFA, baby lookup, and token refresh use `aionanit`; the Nanit `/babies/{uid}/calendar` request is isolated because `aionanit` 1.12.2 has no equivalent method. [ha-nanit issue #49](https://github.com/wealthystudent/ha-nanit/issues/49) reports both `auto_sleep` and manual `sleep` in calendar results. Validate sleep results on your own accounts before enabling routine writes.

## Follow-up validation

- Inspect how manually logged Nanit naps appear on the account before considering them for optional daytime sync. Compare their type and timestamps with automatic naps and the Nanit app.
- Decide whether optional daytime sync should include manual Nanit naps, then add a synthetic fixture for the confirmed response shape.
- Verify MFA login, token refresh, a completed-night dry run, and Huckleberry interval reads and writes with each account before routine imports.
- Review a historical backfill preview against existing manual Huckleberry entries before enabling writes.

Never put passwords, MFA codes, tokens, or identifying account data in source files or test fixtures.

Licensed under [MIT](LICENSE).
