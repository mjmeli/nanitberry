# nanitberry

Parents who use Nanit for sleep monitoring and Huckleberry for sleep tracking otherwise have to enter the same sleep in both apps. Nanitberry automates that handoff by checking Nanit's sleep data and preparing matching entries in Huckleberry.

Nanitberry can:

- **Automatic overnight sync:** Check for completed sleep every 15 minutes and sync it to Huckleberry.
- **Night schedule matching:** Use each child's Huckleberry night schedule to distinguish night from daytime sleep, or use times you configure.
- **Short wake handling:** Join Nanit sleep segments separated by a short wake. The default gap is 20 minutes; longer wakes remain separate.
- **Optional daytime sync:** Include automatic daytime sleep when enabled. Manually logged Nanit naps are not imported.
- **Safe preview:** Review proposed entries before writing. Nanitberry starts in **dry-run, night-only mode** and protects existing Huckleberry sleep.
- **Sleep corrections:** Revise unchanged entries created by Nanitberry when Nanit supplies updated sleep times. Manual edits and deletions release ownership.

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

   The service does not backfill on startup. It checks recent sleep every 15 minutes throughout the day and night. An interval with a reported end time at or before the check becomes eligible immediately. The wake-gap setting joins nearby sleep segments; it does not delay imports. Nanit may revise the record afterward; unchanged entries owned by Nanitberry can be corrected on later checks. The morning cutoff classifies sleep; it does not delay imports.

The Compose file stores tokens, sync status, and sleep ownership metadata in `./data`. Replace that path if you need a different writable, persistent location.

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
| `WRITE_ENABLED` | `false` | Allow new sleep entries and corrections to unchanged owned entries in Huckleberry |
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

## Sync Behavior

The following sections describe various aspects of how nanitberry syncs data and how it tracks sleep.

### Night and daytime sleep

By default, each child's night starts and morning cutoff come from their Huckleberry profile. To set your own times, use `USE_HUCKLEBERRY_HOURS=false` and configure `NIGHT_START` and `MORNING_CUTOFF`. The rules and examples below use an 8 p.m. to 7 a.m. window.

The window determines whether Nanitberry treats sleep as night or daytime; it does not trim an interval to fit the window. Automatic sleep segments separated by no more than `MAX_WAKE_MINUTES` are combined into one interval, including the wake minutes. Longer gaps remain separate. Nanitberry imports available sleep with a reported end time at the next check, without waiting for the wake gap to pass. Later short-wake continuations can extend the same unchanged owned entry.

Daytime sync is off by default. Set `SYNC_DAYTIME=true` to include automatic Nanit sleep between the morning cutoff and the next night start. Manually logged Nanit naps are not imported. A sleep interval that crosses the night start belongs to the night, so it is not also added as a daytime entry.

See below for some examples, based on an 8 p.m. to 7 a.m. window:

| Nanit sleep | Classification | Interval sent to Huckleberry |
| --- | --- | --- |
| 7:45 p.m.–7:20 a.m. | Night; crosses both boundaries | 7:45 p.m.–7:20 a.m. |
| 7:45 p.m.–10:00 p.m. | Night; overlaps the night window | 7:45 p.m.–10:00 p.m. |
| 11:00 p.m.–7:20 a.m. | Night; ends after the morning cutoff | 11:00 p.m.–7:20 a.m. |
| 11:00 p.m.–6:00 a.m. | Night; inside the window | 11:00 p.m.–6:00 a.m. |
| 7:00 p.m.–7:45 p.m. | Daytime; ends before night starts | Only with daytime sync enabled |
| 7:15 a.m.–8:00 a.m. | Daytime; starts after the morning cutoff | Only with daytime sync enabled |

An interval ending exactly at 8 p.m. is daytime; one starting exactly at 7 a.m. is daytime. A post-cutoff segment can still be part of a night interval when it joins a pre-cutoff segment within `MAX_WAKE_MINUTES`.

### When sleep is imported

The service checks recent sleep every 15 minutes. An interval is ready to import as soon as Nanit supplies an end time at or before the check. There is no additional wake-gap delay. This applies to both night and daytime sleep, and to corrections of owned entries.

`MAX_WAKE_MINUTES` controls **joining**, not import timing. If sleep resumes within that gap, later polls combine the available segments and update the same unchanged owned Huckleberry entry. A continuation with no end time does not block importing earlier available sleep; Nanitberry does not invent an end time for the open segment. Setting `MAX_WAKE_MINUTES=0` disables joining across positive wake gaps without changing import timing.

A reported end time does not guarantee that Nanit has finalized its data. Imports are provisional and remain eligible for later corrections while ownership is retained, subject to manual-change protection. Manually editing an imported entry stops its automatic corrections. If later source data joins multiple already imported entries, Nanitberry leaves them for review rather than merging or deleting them automatically.

The night window determines classification. Night intervals are eligible regardless of `SYNC_DAYTIME`; daytime intervals are imported only when `SYNC_DAYTIME=true`. As more segments become available, the combined interval can cross a boundary and belong to the night.

For example, with an **8 p.m.–7 a.m. night window** and **`MAX_WAKE_MINUTES=20`**, suppose sleep runs from **8 p.m.–1 a.m.**, followed by a **30-minute wake**, then sleep from **1:30 a.m.–7 a.m.**:

| Time | Import behavior |
| --- | --- |
| 1 a.m. | If Nanit has supplied the end time by this check, import the 8 p.m.–1 a.m. stretch immediately. Otherwise import on the first check that sees it. |
| 1:30 a.m. | The next stretch begins. The 30-minute wake exceeds the 20-minute joining gap, so the stretches remain separate. |
| 7 a.m. | If Nanit has supplied the second end time by this check, import the 1:30 a.m.–7 a.m. stretch immediately. Otherwise import on the first check that sees it. |

With a **10-minute wake** instead, the first stretch can still import at 1 a.m. The resumed segment remains open until Nanit supplies its end. Once that end is available, Nanitberry extends the first unchanged owned entry to include the continuation and wake minutes, without creating a second entry.

Sleep near the morning cutoff follows the same immediate-import rule. For an **8 p.m.–7 a.m. night window** and **`MAX_WAKE_MINUTES=20`**:

| Example | Nanit sleep | Resulting interval(s) | Initial import and later correction |
| --- | --- | --- | --- |
| 10-minute wake across the cutoff | 11 p.m.–6:55 a.m., then 7:05–7:40 a.m. | One night interval: 11 p.m.–7:40 a.m., including the wake | Import through 6:55 a.m. at 7 a.m.; extend through 7:40 a.m. at 7:45 a.m. |
| 35-minute wake across the cutoff | 11 p.m.–6:55 a.m., then 7:30–7:40 a.m. | Night: 11 p.m.–6:55 a.m.; daytime: 7:30–7:40 a.m. | Night at 7 a.m.; daytime at 7:45 a.m. only with `SYNC_DAYTIME=true` |
| Separate daytime nap | 10–11 a.m. | Daytime: 10–11 a.m. | Import at 11 a.m. if the reported end is available by that check, only with `SYNC_DAYTIME=true` |

These times assume Nanit supplies the shown records by the relevant checks, writes are enabled, and there are no conflicting Huckleberry entries or manual changes. Nanit's own reporting delay can make imports later. The morning cutoff classifies sleep; it does not delay imports.

An interval may be imported up to 15 minutes after its end time becomes available, depending on the next scheduled check. Manual `once` and `backfill` runs use the same readiness rules and can import available sleep from a night still underway.

### Scheduler and lookback

The service checks immediately on startup, then at :00, :15, :30, and :45 throughout the day and night. Each check processes the previous calendar date followed by the current date, using the configured `TZ`. Each date covers daytime sleep before that evening and the night beginning that evening, including sleep continuing past the next morning cutoff.

| Check time | Why both dates are checked |
| --- | --- |
| Monday at 10:30 p.m. | Sunday's period is revisited, while Monday's period catches eligible sleep from Monday evening. |
| Tuesday at 1:30 a.m. | Monday's period catches eligible sleep from the night that began Monday. Tuesday's period has not begun yet and is skipped. |
| Tuesday at noon | Monday's period catches any pending overnight sleep; Tuesday's period catches eligible daytime naps when daytime sync is enabled. |

These examples use an 8 p.m.–7 a.m. window. Every interval follows the immediate-import and night/day classification rules described above; the wake gap only controls joining.

The same periods are revisited on later checks so pending sleep and newly available Nanit records can be imported, and unchanged owned entries can be corrected. A failed scheduled run is retried at the next check. The scheduler does not automatically backfill older dates; use an explicit `backfill` for those, including sleep missed during a longer service outage.

### Conflicts with manual sleep

Before importing, Nanitberry compares each proposed interval with Huckleberry sleep for the same child. Any overlap with an unowned entry blocks the entire proposed interval; it does not shorten sleep to fit. Entries that only meet at the start or end time do not overlap. Existing entries from earlier versions are unowned, even when their times match Nanit.

### Corrections and ownership

Nanit can supply an end time while still developing a sleep record, or deliver additional segments later. For new imports, Nanitberry saves the exact Huckleberry document ID, the Nanit child and source range, the last written contents, and the database version in `./data/sleep_ownership.json`. Later polls can revise a single owned entry whose source range still matches or overlaps the proposed Nanit interval. This includes extensions, shorter corrected durations, and adjusted start times. Corrections cannot overlap other Huckleberry sleep. If Nanit joins multiple tracked entries, Nanitberry logs a review warning instead of merging or deleting them. Disappearing Nanit records do not trigger automatic deletion.

Before a correction, the Huckleberry entry must still match both the saved contents and database version. An outside edit (including notes, or an edit returning to the same values) or deletion releases ownership; Nanitberry leaves that entry alone and does not recreate a deleted entry while its tracking remains. A conditional atomic write protects against changes between the final read and write. The sleep entry and applicable `lastSleep` preference are committed together, preserving newer sleep and manually changed preferences. Overlap history is re-read immediately before writing; a newly inserted overlapping entry after that read cannot be excluded by a document-version condition.

### Ownership retention and storage

Ownership metadata is retained for **90 days after creation**. Repeated polls and corrections do not extend that lifetime. The file stores metadata, not a copy of the calendar or logs; storage stays roughly proportional to the number of imports within the retention period. Expiration only removes local tracking: Huckleberry history remains, becomes unowned, and continues to block overlapping imports. Protection for a deleted entry also expires, so an explicit backfill after expiration can recreate that sleep. Back up this file with your data directory; losing it means existing entries cannot be automatically corrected.

If nanitberry cannot read Huckleberry's sleep history, the run stops without writing. When writes are enabled, it checks history again immediately before each new entry.

## Status and troubleshooting

Use `docker compose logs nanitberry` to see scheduled runs and `docker compose run --rm nanitberry python sync.py status` to inspect the latest result. The container health check stays unhealthy until the first successful sync, and reports failed or stale syncs. If Nanit rejects its saved token, run the interactive `login` command again.

Use one running container with a given `./data` directory at a time. The saved tokens, status, and ownership journal live there. Independent containers using separate data directories must not sync the same child.

## Development

### Images

The Compose example uses the published image at `ghcr.io/mjmeli/nanitberry:latest`. Pull it with `docker compose pull`. For a local build, run:

```sh
docker build -t nanitberry:local .
docker compose -f compose.yaml -f compose.local.yaml up
```

The image workflow builds without publishing on pull requests. Branch pushes and version tags publish to GitHub Container Registry; Docker Hub is also updated when the repository's `DOCKERHUB_TOKEN` secret is configured. A private GHCR package must be authenticated before pulling.

Licensed under [MIT](LICENSE).
