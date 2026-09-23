"""Admin pre-order management — same region-based access control as
orders (api/routes/admin_orders.py), mirrored for the new `preorders`
collection so an admin scoped to AU can only see/act on AU pre-orders,
exactly the way they can only see/act on AU orders today. Not hardcoded to
AU: region is just a field value, resolved from src/models/region.py's
market codes, so a future region needs no route change.

Gated by its own `preorders:read`/`preorders:write` scopes (see
src/models/admin_rbac.py) rather than riding on `orders:read`/`orders:write`
— unlike admin_abandoned_carts.py (which deliberately reuses orders:read
because an abandoned cart is inert lead data with no write action of its
own), pre-orders need a genuine, independently grantable/revocable WRITE
action (the status-update route) and carry the same region-fulfilment
posture as orders, so they get their own resource rather than being
folded into an existing one.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from api.bootstrap import AdminPrincipal, PreorderService, cache, require_scope
from api.json_utils import JSONEncoder
from src.models.preorder import PREORDER_STATUSES
from src.models.region import normalize_region_code, normalize_region_codes
from src.services.preorder_service import public_row

router = APIRouter()


def _preorder_region(preorder: Any) -> str:
    """Normalized region bucket a pre-order was actually submitted for —
    mirrors admin_orders.py's _order_region. Accepts either a raw Mongo
    dict or the public_row() projection (both carry a plain `region` key)."""
    region = preorder.get("region") if isinstance(preorder, dict) else getattr(preorder, "region", None)
    return normalize_region_code(region) or "default"


def _enforce_preorder_region(principal: AdminPrincipal, preorder: Any) -> None:
    """A regional admin (principal.regions non-empty, and not root) may
    only see/act on pre-orders submitted in one of their assigned regions.
    404s rather than 403s — same "don't reveal existence" posture as
    admin_orders.py's _enforce_order_region. Root and any admin with no
    regions assigned are unrestricted."""
    if not principal.regions or principal.is_root:
        return
    allowed = normalize_region_codes(list(principal.regions))
    if _preorder_region(preorder) not in allowed:
        raise HTTPException(status_code=404, detail="Preorder not found")


def _region_filter(
    principal: AdminPrincipal, requested_region: Optional[str] = None
) -> Optional[List[str]]:
    """What to force the pre-order list/export query to for a region-scoped
    admin — None means "no forced filter" (root, or an admin with no
    regions assigned). `requested_region` (the client's own filter
    selection) narrows the result to just that region WHEN it's one the
    admin already holds; a region outside their scope is ignored (can't
    widen), falling back to their full region set. Identical shape/
    behavior to admin_orders.py's _region_filter."""
    if not principal.regions or principal.is_root:
        return None
    allowed = normalize_region_codes(list(principal.regions))
    if requested_region:
        narrowed = normalize_region_code(requested_region) or requested_region.strip().lower()
        if narrowed in allowed:
            return [narrowed]
    return allowed


@router.get("/api/admin/preorders")
async def admin_list_preorders(
    skip: int = 0,
    limit: int = 50,
    status: Optional[str] = None,
    search: Optional[str] = None,
    region: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    principal: AdminPrincipal = Depends(require_scope("preorders", "read")),
):
    """List pre-orders for the admin panel, region-scoped exactly like
    admin_list_orders. Cache-aside (60s TTL), keyed by every filter param —
    same pattern/TTL as admin_orders.py/admin_abandoned_carts.py. Writes
    (status update, public create) don't actively bust this cache, same as
    admin_orders.py's own precedent of just letting the 60s TTL expire."""
    if PreorderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    forced_regions = _region_filter(principal, region)
    if forced_regions:
        region = None

    cache_key = (
        f"admin:preorders:{skip}:{limit}:{status}:{search}:{region}:{from_date}:{to_date}:"
        f"{','.join(sorted(forced_regions)) if forced_regions else ''}"
    )
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = PreorderService()
    try:
        docs = await service.list(
            skip=skip, limit=limit, status=status, search=search,
            from_date=from_date, to_date=to_date, region=region, regions=forced_regions,
        )
        total = await service.count(
            status=status, search=search, from_date=from_date, to_date=to_date,
            region=region, regions=forced_regions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    payload = json.dumps(
        {"data": [public_row(d) for d in docs], "count": total}, cls=JSONEncoder
    )
    if cache:
        await cache.set(cache_key, payload, 60)
    return JSONResponse(content=json.loads(payload))


@router.patch("/api/admin/preorders/{preorder_id}/status")
async def admin_update_preorder_status(
    preorder_id: str,
    payload: Dict[str, Any],
    principal: AdminPrincipal = Depends(require_scope("preorders", "write")),
):
    if PreorderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    status = (payload or {}).get("status")
    if status not in PREORDER_STATUSES:
        raise HTTPException(
            status_code=422, detail=f"status must be one of {sorted(PREORDER_STATUSES)}"
        )

    service = PreorderService()
    existing = await service.get_by_id(preorder_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Preorder not found")
    _enforce_preorder_region(principal, existing)

    updated = await service.update_status(preorder_id, status, actor=principal.email)
    if not updated:
        raise HTTPException(status_code=404, detail="Preorder not found")
    return JSONResponse(content=json.loads(json.dumps(public_row(updated), cls=JSONEncoder)))


@router.get("/api/admin/preorders/export.csv")
async def admin_export_preorders_csv(
    status: Optional[str] = None,
    search: Optional[str] = None,
    region: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    principal: AdminPrincipal = Depends(require_scope("preorders", "read")),
):
    """Same filters + same region scoping as the list route — an AU-scoped
    admin can only ever export AU rows regardless of what `region` they
    pass."""
    if PreorderService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    forced_regions = _region_filter(principal, region)
    if forced_regions:
        region = None

    service = PreorderService()
    docs = await service.list(
        skip=0, limit=10_000, status=status, search=search,
        from_date=from_date, to_date=to_date, region=region, regions=forced_regions,
    )

    columns = [
        "name", "email", "phone", "product_name", "quantity", "size",
        "notify_via", "message", "status", "region", "created_at",
    ]

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for doc in docs:
            row = public_row(doc)
            writer.writerow([
                row.get("name"),
                row.get("email"),
                row.get("phone"),
                row.get("product_name"),
                row.get("quantity"),
                row.get("size"),
                "|".join(row.get("notify_via") or []),
                row.get("message"),
                row.get("status"),
                row.get("region"),
                row.get("created_at").isoformat() if row.get("created_at") else "",
            ])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="preorders.csv"'},
    )
