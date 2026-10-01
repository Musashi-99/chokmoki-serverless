"""Durable record of every background job (`background_jobs` collection).

The Redis stream only carries "run job X"; this document is the source of
truth for status, progress, retry state and the activity timeline an admin
watches. Progress is persisted per batch with a heartbeat, which is what
makes a job resumable after a worker restart and detectable as stuck."""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from bson import ObjectId
from pymongo import ReturnDocument

from src.config import settings
from src.database.connection import db

COLLECTION_NAME = "background_jobs"
ACTIVE_STATUSES = ("queued", "running", "retrying")
TERMINAL_STATUSES = ("succeeded", "partial", "failed")
# A running job whose heartbeat is older than this is considered abandoned
# (worker crashed/redeployed) and may be claimed and resumed.
STALE_AFTER_SECONDS = 90
# A queued job nobody picked up for this long no longer blocks a new one.
QUEUED_GRACE_SECONDS = 15 * 60
MAX_EVENTS = 200
RETENTION_DAYS = 90
HEAVY_FIELDS = {"events": 0, "product_ids": 0, "only_product_ids": 0, "failed_product_ids": 0}


class JobBusyError(Exception):
    """The job is being processed by a live worker; leave the message pending."""


def _now() -> datetime:
    return datetime.utcnow()


class BackgroundJobService:
    async def _collection(self):
        database = await db.get_database()
        return database[COLLECTION_NAME]

    async def ensure_indexes(self) -> None:
        collection = await self._collection()
        await collection.create_index([("created_at", -1)])
        await collection.create_index([("type", 1), ("created_at", -1)])
        await collection.create_index(
            "created_at", name="created_at_ttl", expireAfterSeconds=RETENTION_DAYS * 86400
        )

    async def create(
        self,
        job_type: str,
        created_by: Optional[str],
        *,
        only_product_ids: Optional[List[str]] = None,
        retry_of: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _now()
        doc = {
            "type": job_type,
            "status": "queued",
            "created_by": created_by,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "heartbeat_at": now,
            "total": 0,
            "processed": 0,
            "failed_count": 0,
            "chunks_total": 0,
            "chunks_done": 0,
            "next_chunk": 0,
            "attempt": 0,
            "max_attempts": max(1, settings.fb_sync_max_attempts),
            "next_retry_at": None,
            "last_error": None,
            "product_ids": [],
            "only_product_ids": only_product_ids,
            "failed_product_ids": [],
            "retry_of": retry_of,
            "events": [{"at": now, "level": "info", "message": "Job queued"}],
        }
        collection = await self._collection()
        result = await collection.insert_one(doc)
        doc["_id"] = result.inserted_id
        return doc

    async def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        if not ObjectId.is_valid(job_id):
            return None
        collection = await self._collection()
        return await collection.find_one({"_id": ObjectId(job_id)})

    async def list(self, job_type: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        collection = await self._collection()
        query: Dict[str, Any] = {"type": job_type} if job_type else {}
        cursor = collection.find(query, HEAVY_FIELDS).sort("created_at", -1).limit(max(1, min(limit, 100)))
        return await cursor.to_list(length=limit)

    async def find_active(self, job_type: str) -> Optional[Dict[str, Any]]:
        now = _now()
        collection = await self._collection()
        return await collection.find_one(
            {
                "type": job_type,
                "$or": [
                    {"status": "queued", "created_at": {"$gte": now - timedelta(seconds=QUEUED_GRACE_SECONDS)}},
                    {
                        "status": {"$in": ["running", "retrying"]},
                        "heartbeat_at": {"$gte": now - timedelta(seconds=STALE_AFTER_SECONDS)},
                    },
                ],
            },
            sort=[("created_at", -1)],
        )

    async def claim(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Atomically take a queued job, or resume an abandoned one. Exactly
        one worker wins; the rest get None."""
        if not ObjectId.is_valid(job_id):
            return None
        now = _now()
        collection = await self._collection()
        return await collection.find_one_and_update(
            {
                "_id": ObjectId(job_id),
                "$or": [
                    {"status": "queued"},
                    {
                        "status": {"$in": ["running", "retrying"]},
                        "heartbeat_at": {"$lt": now - timedelta(seconds=STALE_AFTER_SECONDS)},
                    },
                ],
            },
            {"$set": {"status": "running", "heartbeat_at": now}},
            return_document=ReturnDocument.AFTER,
        )

    async def patch(
        self,
        job_id: str,
        fields: Optional[Dict[str, Any]] = None,
        *,
        event: Optional[tuple] = None,
    ) -> None:
        """Update fields (always refreshing the heartbeat) and optionally
        append a (level, message) activity event."""
        update: Dict[str, Any] = {"$set": {"heartbeat_at": _now(), **(fields or {})}}
        if event is not None:
            level, message = event
            update["$push"] = {
                "events": {"$each": [{"at": _now(), "level": level, "message": message}], "$slice": -MAX_EVENTS}
            }
        collection = await self._collection()
        await collection.update_one({"_id": ObjectId(job_id)}, update)
