# nanitberry

Parents who use Nanit for sleep monitoring and Huckleberry for sleep tracking otherwise have to enter the same sleep in both apps. Nanitberry automates that handoff by checking Nanit's sleep data and preparing matching entries in Huckleberry.

Nanitberry can:

- **Automatic overnight sync:** Check for completed sleep every 15 minutes and sync it to Huckleberry.
- **Night schedule matching:** Use each child's Huckleberry night schedule to distinguish night from daytime sleep, or use times you configure.
- **Short wake handling:** Join Nanit sleep segments separated by a short wake. The default gap is 20 minutes; longer wakes remain separate.
- **Optional daytime sync:** Include automatic daytime sleep when enabled. Manually logged Nanit naps are not imported.
- **Safe preview:** Review proposed entries before writing. Nanitberry starts in **dry-run, night-only mode** and skips any entry that overlaps existing Huckleberry sleep.

Review a preview against an existing night before enabling writes. Nanitberry never writes to Huckleberry until you opt in.

## Quick start with Docker Compose

1. If you wish to use the `.env` file configuration, copy `.env.example` to `.env` and add your Nanit and Huckleberry email addresses and passwords. Keep `.env` private. Alternatively, you may provide the environment variables via the docker compose or run command directly.

2. Pull the image and complete Nanit's interactive MFA login:

   ```sh
   docker compose pull
   docker compose run --rm -it nanitberry python sync.py login
   ```

   You may also start the service and run the login command in its container instead:

   ```sh
   docker compose up -d
   docker compose exec nanitberry python sync.py login
   ```

3. If Nanit and Huckleberry each have one child in each account, nanitberry selects them automatically. If either account has more than one child, map the Nanit and Huckleberry child IDs as described under [Child selection](#child-selection).

4. Preview a completed night with writes still disabled:

   ```sh
   docker compose run --rm nanitberry python sync.py once --date YYYY-MM-DD
   ```

   Use the date the night began. Omit `--date` to preview the previous night. Check the proposed sleep times in both apps before enabling writes. You may also execute this command by attaching to the container.

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

When you first launch nanitberry, it will log the detected child IDs for you to retrieve. Alternatively, you can find the IDs directly by running:

```sh
docker compose run --rm nanitberry python sync.py babies
docker compose run --rm nanitberry python sync.py children
```

The Nanit `babies` command needs the saved token from the login step. `children` lists Huckleberry children. Put the JSON value in `.env` as `CHILD_UID_MAP='{"nanit-uid-1":"huckleberry-uid-1"}'`, in your shell, or in `compose.yaml` as `CHILD_UID_MAP: '{"nanit-uid-1":"huckleberry-uid-1"}'`. Each ID can appear only once.

## Night and daytime sleep

By default, each child's night starts and morning cutoff come from their Huckleberry profile. To set your own times, use `USE_HUCKLEBERRY_HOURS=false` and configure `NIGHT_START` and `MORNING_CUTOFF`. The rules and examples below use an 8 p.m. to 7 a.m. window.

The window determines whether Nanitberry treats sleep as night or daytime; it does not trim an interval to fit the window. Automatic sleep segments separated by no more than `MAX_WAKE_MINUTES` are combined into one interval, including the wake minutes. Longer gaps remain separate. Nanitberry waits for a segment to finish and for its wake-gap period to pass before importing it.

Daytime sync is off by default. Set `SYNC_DAYTIME=true` to include automatic Nanit sleep between the morning cutoff and the next night start. Manually logged Nanit naps are not imported. A sleep interval that crosses the night start belongs to the night, so it is not also added as a daytime entry.

| Nanit sleep | Classification | Interval sent to Huckleberry |
| --- | --- | --- |
| 7:45 p.m.–7:20 a.m. | Night; crosses both boundaries | 7:45 p.m.–7:20 a.m. |
| 7:45 p.m.–10:00 p.m. | Night; overlaps the night window | 7:45 p.m.–10:00 p.m. |
| 11:00 p.m.–7:20 a.m. | Night; ends after the morning cutoff | 11:00 p.m.–7:20 a.m. |
| 11:00 p.m.–6:00 a.m. | Night; inside the window | 11:00 p.m.–6:00 a.m. |
| 7:00 p.m.–7:45 p.m. | Daytime; ends before night starts | Only with daytime sync enabled |
| 7:15 a.m.–8:00 a.m. | Daytime; starts after the morning cutoff | Only with daytime sync enabled |

An interval ending exactly at 8 p.m. is daytime; one starting exactly at 7 a.m. is daytime. A post-cutoff segment can still be part of a night interval when it joins a pre-cutoff segment within `MAX_WAKE_MINUTES`.

## Conflicts with manual sleep

Before importing, nanitberry compares each proposed interval with Huckleberry sleep for the same child. If even part of the proposed interval overlaps an existing entry, nanitberry skips the entire proposed interval; it does not shorten it to fit around the existing sleep. The rule applies to manual and previously synced sleep. Entries that only meet at the start or end time do not overlap. Nanitberry does not edit or delete existing entries, so if Nanit later changes a sleep record, review and correct Huckleberry manually.

If nanitberry cannot read Huckleberry's sleep history, the run stops without writing. When writes are enabled, it checks history again immediately before each new entry.

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
