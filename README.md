# nanitberry

Nanitberry copies completed automatic sleep from Nanit to Huckleberry. It runs on a schedule and can combine sleep segments separated by a short wake. It starts in **dry-run, night-only mode**; it never writes to Huckleberry until you enable writes.

## Quick start with Docker Compose

1. Copy `.env.example` to `.env` and add your Nanit and Huckleberry email addresses and passwords. Keep `.env` private.

2. Pull the image and complete Nanit's interactive MFA login:

   ```sh
   docker compose pull
   docker compose run --rm -it nanitberry python sync.py login
   ```

   If your infrastructure cannot run an interactive `docker compose run`, start the service and run the login command in its container instead:

   ```sh
   docker compose up -d
   docker compose exec nanitberry python sync.py login
   ```

3. If either account has more than one child, map the Nanit and Huckleberry child IDs as described under [Child selection](#child-selection). With one child in each account, nanitberry selects them automatically.

4. Preview a completed night with writes still disabled:

   ```sh
   docker compose run --rm nanitberry python sync.py once --date YYYY-MM-DD
   ```

   Use the date the night began. Omit `--date` to preview the previous night. Check the proposed sleep times in both apps before enabling writes.

5. When the preview looks right, set `WRITE_ENABLED: "true"` in `compose.yaml`. To import earlier nights, run an explicit backfill; then start or restart the service:

   ```sh
   docker compose run --rm nanitberry python sync.py backfill --days 7
   docker compose up -d
   ```

   The service does not backfill on startup. It checks the previous night every 15 minutes after the morning cutoff. It waits for the configured wake gap to pass before importing a finished sleep, so a brief wake can still be joined to resumed sleep.

The Compose file stores tokens and sync status in `./data`. Replace that path if you need a different writable, persistent location.

## Configuration

Compose reads values from `.env` or your shell. The required Nanit and Huckleberry credentials are listed in `.env.example` and `compose.yaml`. Nanit credentials are used for the interactive login; the saved Nanit token is used for later syncs. Huckleberry credentials are used to authenticate and refresh its saved session.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TZ` | `America/New_York` | Time zone used for sleep windows and scheduled checks |
| `USE_HUCKLEBERRY_HOURS` | `true` | Use each child's night start and morning cutoff from their Huckleberry profile |
| `NIGHT_START` | `18:00` | Night start when `USE_HUCKLEBERRY_HOURS=false` |
| `MORNING_CUTOFF` | `10:00` | Morning cutoff when `USE_HUCKLEBERRY_HOURS=false` |
| `MAX_WAKE_MINUTES` | `20` | Join sleep segments separated by a wake of this length or less; `0` disables joining |
| `SYNC_DAYTIME` | `false` | Also sync automatic sleep during the day |
| `WRITE_ENABLED` | `false` | Allow new sleep entries to be written to Huckleberry |
| `BACKFILL_DAYS` | `7` | Default number of nights for an explicit backfill |
| `HEALTH_MAX_AGE_HOURS` | `36` | Mark the service unhealthy if it has not synced successfully within this many hours |

### Child selection

When both accounts have exactly one child, nanitberry selects that pair automatically. If either account has multiple children, set `CHILD_UID_MAP` to a JSON object pairing Nanit IDs with Huckleberry IDs. Use one pair to sync one child, or add more pairs to sync multiple children:

```text
{"nanit-uid-1":"huckleberry-uid-1","nanit-uid-2":"huckleberry-uid-2"}
```

To find the IDs, run:

```sh
docker compose run --rm nanitberry python sync.py babies
docker compose run --rm nanitberry python sync.py children
```

The Nanit `babies` command needs the saved token from the login step. `children` lists Huckleberry children. Put the JSON value in `.env` as `CHILD_UID_MAP='{"nanit-uid-1":"huckleberry-uid-1"}'`, in your shell, or in `compose.yaml` as `CHILD_UID_MAP: '{"nanit-uid-1":"huckleberry-uid-1"}'`. Each ID can appear only once.

## How night sleep is defined

By default, each child's night begins and ends at the night start and morning cutoff in their Huckleberry profile. You can instead set `USE_HUCKLEBERRY_HOURS=false` and configure `NIGHT_START` and `MORNING_CUTOFF` yourself. For example, an 8 p.m. to 7 a.m. window covers sleep that overlaps that period.

Nanitberry uses the night window to decide which night owns a sleep interval; it keeps the interval's actual start and end times. So sleep from 7:45 p.m. to 7:20 a.m. is included in full. Sleep ending before 8 p.m. or starting after 7 a.m. is daytime sleep. A sleep segment after 7 a.m. can still be joined to the night if it follows a night segment within `MAX_WAKE_MINUTES`.

Adjacent automatic sleep segments separated by no more than `MAX_WAKE_MINUTES` are combined into one interval, including the wake minutes. A longer gap leaves them separate. In-progress sleep is held for a later check, and completed sleep is only imported after the wake-gap period has passed.

## Conflicts with manual sleep

Before importing, nanitberry compares each proposed interval with Huckleberry sleep for the same child. If even part of the proposed interval overlaps an existing entry, nanitberry skips the entire proposed interval; it does not shorten it to fit around the existing sleep. The rule applies to manual and previously synced sleep. Entries that only meet at the start or end time do not overlap. Nanitberry does not edit or delete existing entries, so if Nanit later changes a sleep record, review and correct Huckleberry manually.

If nanitberry cannot read Huckleberry's sleep history, the run stops without writing. When writes are enabled, it checks history again immediately before each new entry.

## Daytime sleep tracking

Daytime sync is off by default. Set `SYNC_DAYTIME=true` to include automatic Nanit sleep between the morning cutoff and the next night start. Manually logged Nanit naps are not imported. A sleep interval that crosses the night start is assigned to the night, so it is not also added as a daytime entry.

## Status and troubleshooting

Use `docker compose logs nanitberry` to see scheduled runs and `docker compose run --rm nanitberry python sync.py status` to inspect the latest result. The container health check stays unhealthy until the first successful sync, and reports failed or stale syncs. If Nanit rejects its saved token, run the interactive `login` command again.

Use one running container with a given `./data` directory at a time. The saved tokens and status files live there.

## Development

### Images

The Compose example uses the published image at `ghcr.io/mjmeli/nanitberry:latest`. Pull it with `docker compose pull`. For a local build, run:

```sh
docker build -t nanitberry:local .
docker compose -f compose.yaml -f compose.local.yaml up
```

The image workflow builds without publishing on pull requests. Branch pushes and version tags publish to GitHub Container Registry; Docker Hub is also updated when the repository's `DOCKERHUB_TOKEN` secret is configured. A private GHCR package must be authenticated before pulling.

Licensed under [MIT](LICENSE).
