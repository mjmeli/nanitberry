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

5. Set `WRITE_ENABLED=true` only when the previews look right. Run `once` or an explicit `backfill` to import selected history, then restart the daily service with the updated configuration:

   ```sh
   docker compose run --rm nanitberry python sync.py backfill --days 7
   docker compose up -d
   ```

   Startup does not backfill. The service processes the previous night each day at `RUN_AT`.

For plain Docker, use `ghcr.io/mjmeli/nanitberry:latest` with `--env-file .env` and a persistent mount at `/data`. The image's default command starts the scheduler. Run a command with `docker run --rm --env-file .env -v nanitberry-state:/data ghcr.io/mjmeli/nanitberry:latest python sync.py ...` (add `-it` for `login`).

## Images

The [image workflow](.github/workflows/build-and-publish-images.yml) tests every PR and builds its image without pushing. Branch pushes and `v*` tags publish to `ghcr.io/mjmeli/nanitberry` with branch, commit, and version tags; `latest` tracks the default branch or a version tag. If the GitHub Actions secret `DOCKERHUB_TOKEN` is configured, the same tags are also published to `mjmeli/nanitberry` on Docker Hub. The token must have permission to push to that Docker Hub repository.

The Compose example uses `NANITBERRY_IMAGE` to override its GHCR default. To build locally, run `docker build -t nanitberry:local .` and set `NANITBERRY_IMAGE=nanitberry:local` in `.env`. A private GHCR package requires Docker authentication to pull; package visibility is configured on GitHub.

## Configuration

Required for sync: `HUCKLEBERRY_EMAIL`, `HUCKLEBERRY_PASSWORD`, and a saved Nanit token pair. `CHILD_UID_MAP` is the only UID setting. Leave it blank to select automatically when **both** accounts have exactly one child. Otherwise, set a JSON object mapping each Nanit UID to its matching Huckleberry UID. Use one entry to sync one selected child, or several entries to sync several children:

```dotenv
CHILD_UID_MAP={"nanit-uid-1":"huckleberry-uid-1","nanit-uid-2":"huckleberry-uid-2"}
```

Each Nanit and Huckleberry UID can appear only once in the map. Every scheduled run, preview, and backfill processes all mapped pairs in order. Preview all pairs before enabling writes; if a later pair fails after an earlier pair was written, rerunning is safe because existing Huckleberry intervals are checked again. `NANIT_EMAIL` and `NANIT_PASSWORD` are needed for interactive login; scheduled runs use the saved token pair. Keep `.env` private. Container logs contain child names and UIDs during discovery.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TZ` | `America/New_York` | IANA time zone for scheduling and sleep windows |
| `RUN_AT` | `11:00` | Daily local run time; choose a time after the morning cutoff |
| `NIGHT_START` | `18:00` | Evening boundary of the night window |
| `MORNING_CUTOFF` | `10:00` | Next morning's end of the night window |
| `MAX_NIGHT_WAKE_MINUTES` | `20` | Bridge gaps up to this length; `0` disables bridging |
| `SYNC_DAYTIME` | `false` | Also consider automatic sleep between morning cutoff and night start |
| `MAX_DAY_WAKE_MINUTES` | `0` | Separate gap threshold for optional daytime sync |
| `WRITE_ENABLED` | `false` | Enable Huckleberry writes |
| `BACKFILL_DAYS` | `7` | Number of completed nights for an explicit `backfill` (1–365) |
| `HEALTH_MAX_AGE_HOURS` | `36` | Mark sync unhealthy when the last success is too old |
| `USE_HUCKLEBERRY_HOURS` | `false` | Read child profile boundaries if both are `HH:MM` |
| `STATE_DIR` | `/data` | Container directory for tokens, status, and the local lock |

A gap bridged by the threshold appears as continuous logged sleep, including the wake minutes. A longer gap remains unlogged. Boundaries clip intervals, so choose hours that cover the intended sleep. Ambiguous or nonexistent local clock boundaries during a daylight-saving change fail with an error. Confirm Huckleberry's profile hour format in a dry run before enabling `USE_HUCKLEBERRY_HOURS`.

## Safety and status

Before writing, nanitberry reads Huckleberry sleep history and skips any proposed interval that overlaps an existing entry, including a manual one. It reads again immediately before each write. A failed history read stops the run. The service does not edit or delete existing sleep; later Nanit corrections require manual review. One container instance should use a state volume at a time.

Use `docker compose run --rm nanitberry python sync.py status` to inspect the latest attempt, or `docker compose ps` for Docker health. The health check stays unhealthy until the first successful run, reports failed runs, and becomes stale after `HEALTH_MAX_AGE_HOURS`. A rejected Nanit refresh token requires another interactive `login`; temporary Nanit failures are reported separately. Rotated tokens are saved to `/data/nanit_tokens.json` with mode `0600`.

Nanit and Huckleberry are unofficial APIs and can change. Authentication, MFA, baby lookup, and token refresh use `aionanit`; the Nanit `/babies/{uid}/calendar` request is isolated because `aionanit` 1.12.2 has no equivalent method. [ha-nanit issue #49](https://github.com/wealthystudent/ha-nanit/issues/49) reports both `auto_sleep` and manual `sleep` in calendar results. Account behavior and live Huckleberry writes still need validation.

## Follow-up validation

- Inspect how manually logged Nanit naps appear on the account before considering them for optional daytime sync. Compare their type and timestamps with automatic naps and the Nanit app.
- Decide whether optional daytime sync should include manual Nanit naps, then add a synthetic fixture for the confirmed response shape.
- Verify MFA login, token refresh, a completed-night dry run, and Huckleberry interval reads and writes with real accounts before routine imports.
- Review a historical backfill preview against existing manual Huckleberry entries before enabling writes.

Never put passwords, MFA codes, tokens, or identifying account data in source files or test fixtures.

Licensed under [MIT](LICENSE).
