from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from typing import Any, Dict, List, Optional
import json
from bson.errors import InvalidId
from pydantic import ValidationError
from api.bootstrap import (
    AdminPrincipal,
    CouponCreate,
    CouponService,
    CouponUpdate,
    build_update_payload,
    require_admin,
    require_scope,
    require_update_fields,
)
from api.json_utils import JSONEncoder
from src.models.region import normalize_region_codes

router = APIRouter()


def _json(content: Any) -> JSONResponse:
    return JSONResponse(content=json.loads(json.dumps(content, cls=JSONEncoder)))


def _assert_coupon_countries_in_scope(
    principal: AdminPrincipal, countries: Optional[List[str]]
) -> None:
    """A regional admin may only create/update a coupon whose `countries`
    list is a subset of their assigned regions. Rejects with 400 (not a
    silent clamp) so the admin sees exactly what was rejected and why.
    Mirrors _enforce_order_region/_region_filter in admin_orders.py.

    An empty/None `countries` list means "valid everywhere" (see
    src/models/coupon.py) — that's MORE access than any single region, not
    less, so a regional admin leaving every checkbox unchecked must be
    rejected too, not treated as "no countries to check"."""
    if principal.is_root or not principal.regions:
        return
    requested = set(normalize_region_codes(countries or []))
    allowed = set(normalize_region_codes(list(principal.regions)))
    if not requested:
        raise HTTPException(
            status_code=400,
            detail="Select at least one of your assigned region(s) for this coupon — "
            "it can't be left valid everywhere.",
        )
    disallowed = requested - allowed
    if disallowed:
        raise HTTPException(
            status_code=400,
            detail=f"Coupon countries outside your assigned region(s): {sorted(disallowed)}",
        )


@router.get("/api/admin/coupons")
async def admin_list_coupons(principal: AdminPrincipal = Depends(require_scope("coupons", "read"))):
    if CouponService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    data = await CouponService().list()
    return _json({"data": data, "count": len(data)})


@router.post("/api/admin/coupons")
async def admin_create_coupon(
    payload: Dict[str, Any], principal: AdminPrincipal = Depends(require_scope("coupons", "write"))
):
    if CouponService is None or CouponCreate is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    try:
        data = CouponCreate(**payload)
        _assert_coupon_countries_in_scope(principal, data.countries)
        coupon = await CouponService().create(data)
    except HTTPException:
        raise
    except ValueError as e:
        if "already exists" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _json(coupon.model_dump(by_alias=True))


@router.put("/api/admin/coupons/{coupon_id}")
async def admin_update_coupon(
    coupon_id: str, payload: Dict[str, Any], principal: AdminPrincipal = Depends(require_scope("coupons", "write"))
):
    if CouponService is None or CouponUpdate is None or CouponCreate is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    try:
        existing = await CouponService().get_by_id(coupon_id)
    except InvalidId:
        raise HTTPException(status_code=404, detail="Coupon not found")
    if not existing:
        raise HTTPException(status_code=404, detail="Coupon not found")

    try:
        update_data = build_update_payload(CouponUpdate, payload)
        require_update_fields(update_data)
        merged = {
            "code": existing.code,
            "type": existing.type,
            "amount": existing.amount,
            "indicator": existing.indicator,
            "product_ids": existing.product_ids,
            "countries": existing.countries,
            "active": existing.active,
        }
        merged.update(update_data)
        CouponCreate(**merged)
        # Validate the merged/FINAL countries (not just the delta) so a
        # regional admin can't leave an existing out-of-region country
        # untouched while editing something else.
        _assert_coupon_countries_in_scope(principal, merged.get("countries"))
        updated = await CouponService().update(coupon_id, update_data)
    except HTTPException:
        raise
    except InvalidId:
        raise HTTPException(status_code=404, detail="Coupon not found")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        if "already exists" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="Coupon not found")
    return _json(updated.model_dump(by_alias=True))


@router.delete("/api/admin/coupons/{coupon_id}")
async def admin_delete_coupon(coupon_id: str, principal: AdminPrincipal = Depends(require_scope("coupons", "write"))):
    if CouponService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    try:
        deleted = await CouponService().delete(coupon_id)
    except InvalidId:
        raise HTTPException(status_code=404, detail="Coupon not found")
    if not deleted:
        raise HTTPException(status_code=404, detail="Coupon not found")
    return {"success": True}
