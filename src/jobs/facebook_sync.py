"""Run one "sync products to the Facebook catalog" job.

Products go out in batches. Each batch retries transient failures with
exponential backoff (see FacebookCatalogService.submit_with_retry); a batch
that still fails is recorded and the job continues, ending `partial` so the
admin can retry just the failed products. Progress + heartbeat are written
after every batch, so a worker restart resumes at the next unfinished batch
instead of starting over."""
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List

from src.config import settings
from src.jobs.job_service import TERMINAL_STATUSES, BackgroundJobService, JobBusyError
from src.plugins.logger import logger
from src.services.facebook_catalog_service import (
    FacebookCatalogError,
    FacebookCatalogService,
    is_configured,
    refresh_review_statuses,
)
from src.services.product_service import ProductService
from src.services.system_log_service import SystemLogService

COMPONENT = "facebook_sync"
HEARTBEAT_EVERY_SECONDS = 20.0
PAUSE_BETWEEN_BATCHES_SECONDS = 0.25


async def run_facebook_sync_job(job_id: str) -> None:
    jobs = BackgroundJobService()
    job = await jobs.get(job_id)
    if job is None or job["status"] in TERMINAL_STATUSES:
        return  # unknown or already finished — nothing to do, safe to ack
    claimed = await jobs.claim(job_id)
    if claimed is None:
        raise JobBusyError(f"Job {job_id} is being processed by another worker")
    try:
        await _execute(jobs, job_id, claimed)
    except Exception as e:  # a bug here must fail the job visibly, not loop forever
        if logger:
            logger.error(f"Facebook sync job {job_id} crashed: {e}")
        await jobs.patch(
            job_id,
            {"status": "failed", "finished_at": datetime.utcnow(), "last_error": str(e)[:500]},
            event=("error", f"Job crashed: {e}"),
        )
        await SystemLogService().log(
            component=COMPONENT, level="error",
            message=f"Facebook sync job crashed: {e}", context={"job_id": job_id},
        )


async def _execute(jobs: BackgroundJobService, job_id: str, job: Dict[str, Any]) -> None:
    products = ProductService()
    logs = SystemLogService()

    if not is_configured():
        await jobs.patch(
            job_id,
            {"status": "failed", "finished_at": datetime.utcnow(), "last_error": "Facebook catalog is not configured"},
            event=("error", "Facebook catalog is not configured on the server"),
        )
        return

    if not job.get("product_ids"):
        ids: List[str] = job.get("only_product_ids") or await products.list_active_ids()
        chunk_size = max(1, settings.fb_sync_chunk_size)
        chunks_total = (len(ids) + chunk_size - 1) // chunk_size
        await jobs.patch(
            job_id,
            {"product_ids": ids, "total": len(ids), "chunks_total": chunks_total,
             "started_at": datetime.utcnow()},
            event=("info", f"Started: {len(ids)} products in {chunks_total} batch(es)"),
        )
        await logs.log(
            component=COMPONENT, level="info",
            message=f"Facebook catalog sync started: {len(ids)} products",
            context={"job_id": job_id, "created_by": job.get("created_by")},
        )
        job = {**job, "product_ids": ids, "total": len(ids), "chunks_total": chunks_total}
    else:
        await jobs.patch(
            job_id,
            event=("warning", f"Resumed after interruption at batch {job.get('next_chunk', 0) + 1}"),
        )

    ids = job["product_ids"]
    chunk_size = max(1, settings.fb_sync_chunk_size)
    chunks = [ids[i : i + chunk_size] for i in range(0, len(ids), chunk_size)]
    max_attempts = max(1, settings.fb_sync_max_attempts)
    processed = int(job.get("processed", 0))
    failed_count = int(job.get("failed_count", 0))
    failed_ids: List[str] = list(job.get("failed_product_ids") or [])
    last_error = job.get("last_error")
    facebook = FacebookCatalogService()

    async def sleep_with_heartbeat(seconds: float) -> None:
        remaining = seconds
        while remaining > 0:
            step = min(HEARTBEAT_EVERY_SECONDS, remaining)
            await asyncio.sleep(step)
            remaining -= step
            await jobs.patch(job_id)

    for index in range(int(job.get("next_chunk", 0)), len(chunks)):
        chunk_ids = chunks[index]
        label = f"Batch {index + 1}/{len(chunks)}"
        chunk_products = await products.get_many(chunk_ids)

        async def on_retry(attempt: int, delay: float, error: FacebookCatalogError) -> None:
            await jobs.patch(
                job_id,
                {"status": "retrying", "attempt": attempt, "last_error": str(error)[:500],
                 "next_retry_at": datetime.utcnow() + timedelta(seconds=delay)},
                event=("warning", f"{label}: attempt {attempt}/{max_attempts} failed ({error}) — retrying in {delay:.0f}s"),
            )

        try:
            if chunk_products:
                await facebook.submit_chunk(chunk_products, on_retry=on_retry, sleep=sleep_with_heartbeat)
            await products.mark_facebook_synced([str(p.id) for p in chunk_products if p.active])
            processed += len(chunk_ids)
            await jobs.patch(
                job_id,
                {"status": "running", "attempt": 0, "next_retry_at": None,
                 "processed": processed, "chunks_done": index + 1, "next_chunk": index + 1},
                event=("info", f"{label} sent ({len(chunk_ids)} products)"),
            )
        except FacebookCatalogError as e:
            last_error = str(e)[:500]
            failed_count += len(chunk_ids)
            failed_ids.extend(chunk_ids)
            await products.set_facebook_error_many(chunk_ids, last_error)
            await jobs.patch(
                job_id,
                {"status": "running", "attempt": 0, "next_retry_at": None, "failed_count": failed_count,
                 "failed_product_ids": failed_ids, "last_error": last_error,
                 "chunks_done": index + 1, "next_chunk": index + 1},
                event=("error", f"{label} failed permanently: {e}"),
            )
        await asyncio.sleep(PAUSE_BETWEEN_BATCHES_SECONDS)

    if failed_count == 0:
        status, level = "succeeded", "info"
        summary = f"Finished: all {processed} products sent to Facebook"
    elif processed == 0:
        status, level = "failed", "error"
        summary = f"Failed: no products were sent ({last_error})"
    else:
        status, level = "partial", "error"
        summary = f"Finished with errors: {processed} sent, {failed_count} failed ({last_error})"
    await jobs.patch(
        job_id,
        {"status": status, "finished_at": datetime.utcnow(), "attempt": 0, "next_retry_at": None},
        event=("error" if level == "error" else "info", summary),
    )
    # level "error" also raises the existing Telegram system-error alert.
    await logs.log(component=COMPONENT, level=level, message=f"Facebook catalog sync: {summary}",
                   context={"job_id": job_id, "processed": processed, "failed": failed_count})

    if processed > 0:
        try:
            checked = await refresh_review_statuses(products)
            await jobs.patch(job_id, event=("info", f"Checked Facebook review status for {checked} items"))
        except Exception as e:
            await jobs.patch(job_id, event=("warning", f"Could not read Facebook review status yet: {e}"))
