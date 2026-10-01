"""Admin endpoints for background jobs (currently: Facebook catalog sync-all).

Starting a job only creates its record and queues it; the worker does the
work, so these requests return immediately however large the catalog is."""
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from api.bootstrap import require_scope_email
from api.json_utils import JSONEncoder
from src.jobs.events import EVENT_FACEBOOK_SYNC_ALL, JOB_TYPE_FACEBOOK_SYNC_ALL, JOBS_STREAM_KEY
from src.jobs.job_service import ACTIVE_STATUSES, TERMINAL_STATUSES, BackgroundJobService
from src.services.facebook_catalog_service import is_configured as facebook_configured
from src.streams import event_bus

router = APIRouter()

_INTERNAL_FIELDS = ("product_ids", "only_product_ids", "failed_product_ids", "next_chunk")


def _serialize(job: Dict[str, Any], *, with_events: bool = True) -> Dict[str, Any]:
    out = {k: v for k, v in job.items() if k not in _INTERNAL_FIELDS}
    if not with_events:
        out.pop("events", None)
    return json.loads(json.dumps(out, cls=JSONEncoder))


async def _enqueue_facebook_sync(
    email: str, *, only_product_ids: Optional[List[str]] = None, retry_of: Optional[str] = None
) -> JSONResponse:
    if not facebook_configured():
        raise HTTPException(status_code=503, detail="Facebook catalog is not configured on the server")

    jobs = BackgroundJobService()
    active = await jobs.find_active(JOB_TYPE_FACEBOOK_SYNC_ALL)
    if active:
        return JSONResponse(
            status_code=200,
            content={"job_id": str(active["_id"]), "already_running": True, "job": _serialize(active)},
        )

    job = await jobs.create(
        JOB_TYPE_FACEBOOK_SYNC_ALL, email, only_product_ids=only_product_ids, retry_of=retry_of
    )
    job_id = str(job["_id"])
    entry_id = await event_bus.publish(JOBS_STREAM_KEY, EVENT_FACEBOOK_SYNC_ALL, {"job_id": job_id})
    if entry_id is None:
        await jobs.patch(
            job_id,
            {"status": "failed", "finished_at": datetime.utcnow(), "last_error": "Could not queue the job"},
            event=("error", "Could not queue the job (queue unavailable)"),
        )
        raise HTTPException(status_code=503, detail="Could not queue the sync job — try again shortly")
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "already_running": False, "job": _serialize(job)},
    )


@router.post("/api/admin/facebook-catalog/sync-all")
async def admin_sync_all_to_facebook(email: str = Depends(require_scope_email("products", "write"))):
    """Queue a sync of every active product (processed by the worker)."""
    return await _enqueue_facebook_sync(email)


@router.get("/api/admin/jobs")
async def admin_list_jobs(
    type: Optional[str] = None,
    limit: int = 20,
    email: str = Depends(require_scope_email("products", "read")),
):
    jobs = await BackgroundJobService().list(type, limit)
    return JSONResponse(content={"data": [_serialize(j, with_events=False) for j in jobs]})


@router.get("/api/admin/jobs/{job_id}")
async def admin_get_job(job_id: str, email: str = Depends(require_scope_email("products", "read"))):
    job = await BackgroundJobService().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(content=_serialize(job))


@router.post("/api/admin/jobs/{job_id}/retry-failed")
async def admin_retry_failed_job(job_id: str, email: str = Depends(require_scope_email("products", "write"))):
    """Queue a new job covering only the products that failed in this one."""
    job = await BackgroundJobService().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="That job is still running")
    failed_ids = job.get("failed_product_ids") or []
    if not failed_ids:
        raise HTTPException(status_code=409, detail="That job has no failed products to retry")
    return await _enqueue_facebook_sync(email, only_product_ids=list(failed_ids), retry_of=str(job["_id"]))
