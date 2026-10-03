"""Huckleberry sleep storage adapter for the pinned unofficial client.

Keep private client methods and Firestore details here until public history
methods provide the required identity and conditional-write semantics.
Ownership, Nanit joining, retention, and conflict policy belong in sync.py.
"""
from dataclasses import dataclass, field


class SleepWriteConflict(Exception):
    """The database rejected a write because a target changed or disappeared."""


@dataclass(frozen=True)
class SleepInterval:
    start: float
    duration: float
    document_id: str


@dataclass(frozen=True)
class SleepRecord:
    payload: dict
    version: str
    _update_time: object = field(repr=False)


class PreparedSleepWrite:
    """Prepare reads first; callers can persist intent before committing."""

    def __init__(self, batch):
        self._batch = batch

    async def commit(self):
        from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound

        try:
            results = await self._batch.commit()
        except (AlreadyExists, FailedPrecondition, NotFound) as exc:
            raise SleepWriteConflict() from exc
        # Keep nanoseconds when persisting the database's exact revision.
        return results[0].update_time.rfc3339()


class HuckleberrySleepAdapter:
    def __init__(self, api):
        self.api = api

    async def list_intervals(self, child_uid, start, end):
        """Read all overlaps, including legacy batches; propagate read errors."""
        from google.cloud import firestore
        from huckleberry_api.firebase_types import FirebaseSleepIntervalData, FirebaseSleepMultiContainer

        client = await self.api._get_firestore_client()
        collection = client.collection("sleep").document(child_uid).collection("intervals")
        start_ts, end_ts = start.timestamp(), end.timestamp()
        results = []
        regular = collection.where(filter=firestore.FieldFilter("start", "<", end_ts)).stream()
        async for doc in regular:
            data = doc.to_dict()
            if data and not data.get("multi"):
                interval = FirebaseSleepIntervalData.model_validate(data)
                if float(interval.start) + float(interval.duration) > start_ts:
                    results.append(SleepInterval(start=interval.start, duration=interval.duration,
                                                 document_id=doc.id))
        multi = collection.where(filter=firestore.FieldFilter("multi", "==", True)).stream()
        async for doc in multi:
            data = doc.to_dict()
            if data:
                container = FirebaseSleepMultiContainer.model_validate(data)
                results.extend(SleepInterval(start=entry.start, duration=entry.duration,
                                             document_id=doc.id) for entry in container.data.values()
                               if float(entry.start) < end_ts
                               and float(entry.start) + float(entry.duration) > start_ts)
        return results

    async def read_record(self, child_uid, document_id):
        client = await self.api._get_firestore_client()
        ref = client.collection("sleep").document(child_uid).collection("intervals").document(document_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            return None
        return SleepRecord(snapshot.to_dict(), snapshot.update_time.rfc3339(), snapshot.update_time)

    async def build_payload(self, start, end, now_ts, previous=None):
        payload = dict(previous) if previous is not None else {
            "offset": await self.api._get_timezone_offset_minutes(),
        }
        start_sec, end_sec = int(start.timestamp()), int(end.timestamp())
        if end_sec <= start_sec:
            raise ValueError("Sleep end must be after start")
        payload.update(start=start_sec, duration=end_sec - start_sec,
                       end_offset=payload["offset"], lastUpdated=now_ts)
        return payload

    async def prepare_write(self, child_uid, document_id, payload, previous=None):
        """Conditionally write the exact row and applicable lastSleep atomically.

        A supplied record is the expected revision for an update. Without one,
        create requires that the chosen document ID is absent. Newer lastSleep
        and manually changed cached values are preserved.
        """
        from google.cloud.firestore_v1 import LastUpdateOption

        client = await self.api._get_firestore_client()
        root = client.collection("sleep").document(child_uid)
        ref = root.collection("intervals").document(document_id)
        parent = await root.get()
        parent_data = parent.to_dict() or {}
        last = (parent_data.get("prefs") or {}).get("lastSleep") or {}
        old = previous.payload if previous is not None else None
        update_last = (not last or float(last.get("start", -1)) < payload["start"]
                       or (old is not None and all(last.get(k) == old.get(k)
                                                  for k in ("start", "duration", "offset"))))
        batch = client.batch()
        if previous is not None:
            batch.update(ref, payload, option=LastUpdateOption(previous._update_time))
        else:
            batch.create(ref, payload)
        if update_last:
            last_payload = {k: payload[k] for k in ("start", "duration", "offset")}
            now_ts = payload["lastUpdated"]
            if parent.exists:
                batch.update(root, {"prefs.lastSleep": last_payload,
                                    "prefs.timestamp": {"seconds": now_ts},
                                    "prefs.local_timestamp": now_ts},
                             option=LastUpdateOption(parent.update_time))
            else:
                batch.create(root, {"prefs": {"lastSleep": last_payload,
                                             "timestamp": {"seconds": now_ts},
                                             "local_timestamp": now_ts}})
        return PreparedSleepWrite(batch)
