"""Admin visibility into backend operations — payment reconciliation runs,
unresolved payment attempts, the system_logs operational log, and a live
observability snapshot (circuit breakers, stream lag, DLQ depth). Read-only:
audit/inspection views only, the actual work happens elsewhere.
"""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from typing import Any, Dict, Optional
from api.bootstrap import (
    PaymentReconciliationService,
    SystemLogService,
    redis_client,
    require_admin,
    require_scope_email,
)
from api.json_utils import _json_response_content

router = APIRouter()

# The three named breakers registered across the codebase (order_service /
# payment_reconciliation_service, shiprocket/client, telegram_service). Their
# authoritative state lives in Redis under breaker:{name}:* — reading it here
# is cross-process truth (backend + worker), unlike the per-process Prometheus
# gauge which only reflects what one process last observed.
BREAKER_NAMES = ("razorpay", "shiprocket", "telegram")


@router.get("/api/admin/payment-reconciliation/runs")
async def admin_list_reconciliation_runs(
    limit: int = 50,
    email: str = Depends(require_scope_email("orders", "read")),
):
    if PaymentReconciliationService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    runs = await PaymentReconciliationService().list_runs(limit=limit)
    return JSONResponse(content=_json_response_content({"data": runs}))


@router.get("/api/admin/payment-reconciliation/attempts")
async def admin_list_payment_attempts(
    status: Optional[str] = None,
    limit: int = 100,
    email: str = Depends(require_scope_email("orders", "read")),
):
    if PaymentReconciliationService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    attempts = await PaymentReconciliationService().list_attempts(status=status, limit=limit)
    return JSONResponse(content=_json_response_content({"data": attempts}))


@router.get("/api/admin/system-logs")
async def admin_list_system_logs(
    component: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 100,
    email: str = Depends(require_scope_email("audit", "read")),
):
    """Durable operational log — fallback triggers, detected inconsistencies,
    degraded-mode operation. See src/services/system_log_service.py.
    """
    if SystemLogService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    logs = await SystemLogService().list_logs(component=component, level=level, limit=limit)
    return JSONResponse(content=_json_response_content({"data": logs}))


@router.get("/api/admin/system-logs/summary")
async def admin_system_logs_summary(email: str = Depends(require_scope_email("audit", "read"))):
    """Distinct components + per-level counts — powers the System Logs
    page's filter dropdowns without hardcoding component names in the UI.
    """
    if SystemLogService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    summary = await SystemLogService().summary()
    return JSONResponse(content=_json_response_content(summary))


@router.get("/api/admin/observability")
async def admin_observability(email: str = Depends(require_scope_email("audit", "read"))):
    """Live operational snapshot, read from the shared cross-process state
    (Redis) rather than this process's own Prometheus registry — so it
    reflects the backend AND worker together:

    - circuit breakers: open/closed + current failure count per dependency
    - Redis Streams: length, consumer-group pending (lag), and DLQ depth —
      a growing pending count means the worker is behind or down; anything
      in a DLQ is an event that permanently failed processing.
    """
    if redis_client is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    redis = await redis_client.get_client()

    breakers: Dict[str, Any] = {}
    for name in BREAKER_NAMES:
        try:
            is_open = bool(await redis.exists(f"breaker:{name}:open"))
            failures = await redis.get(f"breaker:{name}:failures")
            open_ttl = await redis.ttl(f"breaker:{name}:open") if is_open else None
            breakers[name] = {
                "open": is_open,
                "recent_failures": int(failures or 0),
                "cooldown_remaining_seconds": open_ttl if is_open and open_ttl and open_ttl > 0 else None,
            }
        except Exception as e:
            breakers[name] = {"error": str(e)}

    async def stream_stats(stream_key: str, group: str) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}
        try:
            stats["length"] = await redis.xlen(stream_key)
        except Exception:
            stats["length"] = 0
        try:
            # 'pending' = delivered to the group but not yet acked — the
            # real backlog/lag signal for the worker's consumers.
            pending = await redis.xpending(stream_key, group)
            stats["pending"] = pending.get("pending", 0) if isinstance(pending, dict) else 0
        except Exception:
            # Group doesn't exist yet (no consumer has ever run) — not an error.
            stats["pending"] = None
        try:
            stats["dlq_length"] = await redis.xlen(f"{stream_key}:dlq")
        except Exception:
            stats["dlq_length"] = 0
        return stats

    streams = {
        "orders": await stream_stats("chokmoki:orders:events", "orders"),
        "alerts": await stream_stats("chokmoki:alerts:stream", "alerts"),
    }

    return JSONResponse(content=_json_response_content({
        "breakers": breakers,
        "streams": streams,
    }))
