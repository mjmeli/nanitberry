"""Command-line entry point for Nanit to Huckleberry sleep sync."""
import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

import clients
import config
import service
import storage


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("login", "once", "backfill", "serve", "babies", "children", "status", "healthcheck"))
    parser.add_argument("--date", type=date.fromisoformat, help="Latest evening date, YYYY-MM-DD (once/backfill)")
    parser.add_argument("--days", type=int, help="Number of nights (backfill; otherwise BACKFILL_DAYS)")
    args = parser.parse_args()
    settings = config.Settings.from_env()
    paths = config.DEFAULT_PATHS
    if args.command == "login":
        asyncio.run(clients.nanit_login(settings=settings, paths=paths))
    elif args.command == "babies":
        async def show_babies():
            async with aiohttp.ClientSession() as session:
                for baby in await clients.restore_nanit(session, paths=paths).async_get_babies():
                    print(baby.name, baby.uid)
        asyncio.run(show_babies())
    elif args.command == "children":
        asyncio.run(clients.huckleberry_children(settings=settings, paths=paths))
    elif args.command == "status":
        healthy, message = storage.health_status(paths=paths)
        print(json.dumps({"healthy": healthy, "message": message,
                          "status": json.loads(paths.status.read_text()) if paths.status.exists() else None}, indent=2))
    elif args.command == "healthcheck":
        healthy, message = storage.health_status(paths=paths)
        print(message)
        if not healthy:
            sys.exit(1)
    elif args.command == "once":
        tz = ZoneInfo(settings.timezone)
        asyncio.run(service.sync_day(args.date or datetime.now(tz).date() - timedelta(days=1), settings=settings, paths=paths))
    elif args.command == "backfill":
        tz = ZoneInfo(settings.timezone)
        latest = args.date or datetime.now(tz).date() - timedelta(days=1)
        days = args.days if args.days is not None else int(config.setting("BACKFILL_DAYS", "7"))
        asyncio.run(service.backfill(latest, days, settings=settings, paths=paths))
    else:
        asyncio.run(clients.log_missing_uids(settings=settings, paths=paths))
        try:
            prior = json.loads(paths.status.read_text()) if paths.status.exists() else {}
        except (OSError, ValueError):
            prior = {}
        if not isinstance(prior, dict) or prior.get("state") != "error":
            storage.status_update("waiting", "scheduler_started", service_started=storage.utc_now(), paths=paths)
        try:
            asyncio.run(service.scheduled(settings=settings, paths=paths))
        except Exception as exc:
            reason, detail = service.failure_reason(exc)
            storage.status_update("error", reason, detail, paths=paths)
            raise


if __name__ == "__main__":
    main()
