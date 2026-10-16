"""Public enquiry submission — POST /api/enquiries.

Opened on the shop laptop by an admin (or by anyone with the link), so it is
unauthenticated; abuse is handled by the rate-limit rule in
config/rate_limits.yaml plus the honeypot field in the payload."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api.bootstrap import ProductService, logger
from src.models.enquiry import EnquiryCreateInput
from src.security.client_ip import get_client_ip
from src.services.enquiry_service import EnquiryService

router = APIRouter()


@router.post("/api/enquiries")
async def api_create_enquiry(request: Request, payload: Dict[str, Any]):
    try:
        data = EnquiryCreateInput(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())

    if data.is_spam:
        # Pretend success so a bot learns nothing, but store nothing.
        return JSONResponse(content={"id": "received"}, status_code=201)

    product_name = ""
    product_slug = ""
    if data.productId:
        product = await ProductService().get_by_id(data.productId) if ProductService else None
        if not product or not getattr(product, "active", True):
            raise HTTPException(status_code=404, detail="Product not found")
        product_name = getattr(product, "name", "") or ""
        product_slug = getattr(product, "slug", "") or ""

    ip = get_client_ip(request) if get_client_ip else None
    try:
        doc = await EnquiryService().record(
            data,
            product_name=product_name,
            product_slug=product_slug,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        if logger:
            logger.error(f"Enquiry capture failed: {exc}")
        raise HTTPException(status_code=500) from exc

    # Nothing but the new row's id goes back to a public caller.
    return JSONResponse(content={"id": str(doc.get("_id"))}, status_code=201)
