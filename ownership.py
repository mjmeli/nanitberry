"""Durable ownership journal and safe reconciliation of imported sleep."""
import json
import logging
import math
import uuid

import config
import storage
import huckleberry_sleep
from huckleberry_sleep import HuckleberrySleepAdapter, SleepWriteConflict

LOG = logging.getLogger("nanit_huckleberry_sync")
OWNERSHIP_RETENTION_DAYS = 90


class SleepOwnership:
    """Local write journal; only unchanged, explicitly owned rows may be revised."""

    def __init__(self, now_ts, *, paths=None):
        self.path = (paths or config.DEFAULT_PATHS).ownership
        self.now_ts = now_ts
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.data = {"version": 1, "records": {}}
        # Corrupt ownership must fail closed, never silently reset ownership.
        if (not isinstance(self.data, dict) or self.data.get("version") != 1
                or not isinstance(self.data.get("records"), dict)):
            raise ValueError("Invalid sleep ownership journal")
        for key, row in list(self.data["records"].items()):
            if (not isinstance(row, dict) or row.get("state") not in ("owned", "pending", "released")
                    or not isinstance(row.get("payload"), dict)
                    or not all(field in row for field in ("created_at", "nanit_uid", "child_uid", "source_start", "source_end"))
                    or (row["state"] == "owned" and not isinstance(row.get("version"), str))):
                raise ValueError("Invalid sleep ownership record")
            if (not all(isinstance(row[field], (int, float)) and math.isfinite(row[field])
                        for field in ("created_at", "source_start", "source_end"))
                    or row["source_end"] <= row["source_start"]
                    or not all(field in row["payload"] for field in ("start", "duration", "offset"))):
                raise ValueError("Invalid sleep ownership record")
            if float(row["created_at"]) < now_ts - OWNERSHIP_RETENTION_DAYS * 86400:
                del self.data["records"][key]

    def save(self):
        storage.private_json(self.path, self.data, indent=2)

    def candidates(self, nanit_uid, child_uid, span):
        a, b = (point.timestamp() for point in span)
        return [(key, row) for key, row in self.data["records"].items()
                if row["nanit_uid"] == nanit_uid and row["child_uid"] == child_uid
                and (a == row["source_start"]
                     or (a < row["source_end"] and row["source_start"] < b))]

    async def sync_span(self, api, nanit_uid, child_uid, span, *, write, kind):
        matches = self.candidates(nanit_uid, child_uid, span)
        if len(matches) > 1:
            LOG.warning("Review %s–%s: Nanit now joins multiple tracked entries", *span)
            return
        key, row = matches[0] if matches else (uuid.uuid4().hex[:16], None)
        if row and row["state"] != "owned":
            LOG.warning("Review %s–%s: ownership released or write outcome uncertain", *span)
            if write and row["state"] == "pending":
                row["state"] = "released"
                self.save()
            return
        adapter = HuckleberrySleepAdapter(api)
        snapshot = await adapter.read_record(child_uid, key) if row else None
        if row and (snapshot is None or snapshot.version != row["version"]
                    or snapshot.payload != row["payload"]):
            LOG.warning("Review %s–%s: imported Huckleberry entry was changed or deleted", *span)
            if write:
                row["state"] = "released"
                self.save()
            return
        start = int(span[0].timestamp())
        duration = int(span[1].timestamp()) - start
        # Always re-read history before writing; exclude only the exact owned row.
        current = await huckleberry_sleep.strict_sleep_intervals(api, child_uid, *span)
        if any(not row or getattr(entry, "document_id", None) != key for entry in current):
            LOG.warning("Skipping %s–%s: overlaps other Huckleberry sleep", *span)
            return
        if row and row["payload"]["start"] == start and row["payload"]["duration"] == duration:
            return
        action = "UPDATE" if row else "WRITE"
        label = action if write else ("DRY RUN UPDATE" if row else "DRY RUN")
        LOG.info("%s %s–%s (%s)", label, *span, kind)
        if not write:
            return
        payload = await adapter.build_payload(*span, self.now_ts,
                                              previous=row["payload"] if row else None)
        operation = await adapter.prepare_write(child_uid, key, payload, previous=snapshot)
        pending = {"state": "pending", "created_at": row["created_at"] if row else self.now_ts,
                   "nanit_uid": nanit_uid, "child_uid": child_uid,
                   "source_start": span[0].timestamp(), "source_end": span[1].timestamp(),
                   "payload": payload}
        self.data["records"][key] = pending
        self.save()  # Durable intent before any remote write (including timeout/crash).
        try:
            version = await operation.commit()
        except SleepWriteConflict:
            # These errors reject the whole atomic batch; no partial remote writes.
            if row:
                latest = await adapter.read_record(child_uid, key)
                if latest is None or latest.version != row["version"]:
                    row["state"] = "released"
                self.data["records"][key] = row
            else:
                pending["state"] = "released"
            self.save()
            LOG.warning("Skipping %s–%s: Huckleberry changed during conditional write", *span)
            return
        pending.update(state="owned", version=version)
        self.save()

