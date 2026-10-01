from __future__ import annotations

from typing import Any, Dict

from src.jobs.events import EVENT_FACEBOOK_SYNC_ALL, JOBS_GROUP, JOBS_STREAM_KEY
from src.jobs.facebook_sync import run_facebook_sync_job
from src.jobs.job_service import BackgroundJobService
from src.plugins.logger import logger
from src.streams.consumer import MAX_DELIVERY_ATTEMPTS, StreamConsumer


class JobConsumer:
    """Worker-side runner for admin-triggered background jobs. Runs in the
    separate `worker` process so a long job never ties up an API request, and
    an API restart can't lose it (the stream entry stays pending until the
    job handler returns)."""

    def __init__(self) -> None:
        self._stream_consumer = StreamConsumer(
            JOBS_STREAM_KEY, JOBS_GROUP, handler=self._dispatch, on_failure=self._on_failure
        )

    async def run(self) -> None:
        await self._stream_consumer.run()

    async def _dispatch(self, event_type: str, payload: Dict[str, Any]) -> None:
        if event_type == EVENT_FACEBOOK_SYNC_ALL:
            await run_facebook_sync_job(payload["job_id"])
        elif logger:
            logger.warning(f"JobConsumer: unrecognized event type '{event_type}'")

    async def _on_failure(self, event_type: str, payload: Dict[str, Any], delivery_count: int, error: Exception) -> None:
        # Last delivery: if the job is still abandoned (stale heartbeat),
        # surface it as failed instead of leaving it "running" forever.
        if delivery_count < MAX_DELIVERY_ATTEMPTS:
            return
        jobs = BackgroundJobService()
        job_id = payload.get("job_id", "")
        if await jobs.claim(job_id):
            await jobs.patch(
                job_id,
                {"status": "failed", "last_error": str(error)[:500]},
                event=("error", f"Worker gave up after {delivery_count} deliveries: {error}"),
            )
