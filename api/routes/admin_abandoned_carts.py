"""Admin listing of captured abandoned carts / guest leads.

Reuses the existing `orders:read` scope — an abandoned cart is pre-order
data, and anyone trusted to read orders is trusted to read this.

Deliberately NOT region-scoped, unlike admin_orders.py: an abandoned cart
carries no fulfilment obligation tied to a region, so narrowing it per
admin would only hide recovery opportunities. The region field is still
returned (and filterable) so an admin can narrow it themselves.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from api.bootstrap import AbandonedCartService, AdminPrincipal, cache, require_scope
from api.json_utils import JSONEncoder

router = APIRouter()


@router.get("/api/admin/abandoned-carts")
async def admin_list_abandoned_carts(
    skip: int = 0,
    limit: int = 30,
    search: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    country: Optional[str] = None,
    converted: Optional[bool] = None,
    principal: AdminPrincipal = Depends(require_scope("orders", "read")),
):
    """Cache-aside (60s), keyed on every param that affects the result —
    same pattern and TTL as admin_list_orders."""
    if AbandonedCartService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = (
        f"admin:abandoned_carts:{skip}:{limit}:{search}:{status}:"
        f"{from_date}:{to_date}:{country}:{converted}"
    )
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = AbandonedCartService()
    try:
        rows = await service.list(
            skip=skip, limit=limit, status=status, search=search,
            from_date=from_date, to_date=to_date, country=country, converted=converted,
        )
        total = await service.count(
            status=status, search=search, from_date=from_date, to_date=to_date,
            country=country, converted=converted,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    payload = json.dumps({"data": rows, "count": total}, cls=JSONEncoder)
    if cache:
        await cache.set(cache_key, payload, 60)
    return JSONResponse(content=json.loads(payload))
