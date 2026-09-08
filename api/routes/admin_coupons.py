from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from typing import Any, Dict
import json
from bson.errors import InvalidId
from pydantic import ValidationError
from api.bootstrap import (
    CouponCreate,
    CouponService,
    CouponUpdate,
    build_update_payload,
    require_admin,
    require_scope_email,
    require_update_fields,
)
from api.json_utils import JSONEncoder

router = APIRouter()


def _json(content: Any) -> JSONResponse:
    return JSONResponse(content=json.loads(json.dumps(content, cls=JSONEncoder)))


@router.get("/api/admin/coupons")
async def admin_list_coupons(email: str = Depends(require_scope_email("orders", "write"))):
    if CouponService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    data = await CouponService().list()
    return _json({"data": data, "count": len(data)})


@router.post("/api/admin/coupons")
async def admin_create_coupon(
    payload: Dict[str, Any], email: str = Depends(require_scope_email("orders", "write"))
):
    if CouponService is None or CouponCreate is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    try:
        data = CouponCreate(**payload)
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
    coupon_id: str, payload: Dict[str, Any], email: str = Depends(require_scope_email("orders", "write"))
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
            "active": existing.active,
        }
        merged.update(update_data)
        CouponCreate(**merged)
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
async def admin_delete_coupon(coupon_id: str, email: str = Depends(require_scope_email("orders", "write"))):
    if CouponService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    try:
        deleted = await CouponService().delete(coupon_id)
    except InvalidId:
        raise HTTPException(status_code=404, detail="Coupon not found")
    if not deleted:
        raise HTTPException(status_code=404, detail="Coupon not found")
    return {"success": True}
