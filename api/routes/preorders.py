"""Public pre-order capture.

Fires from the storefront's pre-order form on a product page whose region
is pre-order-only (currently AU — a separate, already-planned frontend
change, not this route's concern). Region is resolved server-side (see
PreorderService.resolve_region) from the client's selectedCountry + GeoIP,
never trusted verbatim from the request body. Rate limits live in
config/rate_limits.yaml (`preorders`).
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api.bootstrap import PreorderCreateInput, PreorderService, ProductService, logger
from src.security.client_ip import get_client_ip

router = APIRouter()


@router.post("/api/preorders")
async def api_create_preorder(request: Request, payload: Dict[str, Any]):
    if PreorderService is None or PreorderCreateInput is None or ProductService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    try:
        data = PreorderCreateInput(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())

    product = await ProductService().get_by_id(data.productId)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    ip = get_client_ip(request) if get_client_ip else None
    region = await PreorderService.resolve_region(data.selectedCountry, ip)

    try:
        doc = await PreorderService().record(
            data,
            product_name=getattr(product, "name", "") or "",
            product_slug=getattr(product, "slug", "") or "",
            region=region,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        if logger:
            logger.error(f"Preorder capture failed: {exc}")
        raise HTTPException(status_code=500) from exc

    # Don't leak anything beyond this submitter's own id/status — no other
    # customer's data, no internal fields (ip/user_agent/full document).
    return JSONResponse(
        content={"id": str(doc.get("_id")), "status": doc.get("status") or "new"},
        status_code=201,
    )
