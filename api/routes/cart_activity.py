"""Public abandoned-cart capture.

Fires from the checkout form (debounced) as soon as a structurally valid
email or phone has been typed. Deliberately forgiving about everything
else — a half-typed address is the normal payload — but strict about the
one thing that matters: a row is only written when there's a plausible way
to reach the person. Rate limits live in config/rate_limits.yaml
(`cart_activity`).
"""
from typing import Any, Dict
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api.bootstrap import AbandonedCartRecordInput, AbandonedCartService, logger
from src.security.client_ip import get_client_ip
from src.utils.contact_normalize import normalize_phone

router = APIRouter()

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_CART_ITEMS = 50


@router.post("/api/cart-activity")
async def api_record_cart_activity(request: Request, payload: Dict[str, Any]):
    if AbandonedCartService is None or AbandonedCartRecordInput is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    try:
        data = AbandonedCartRecordInput(**payload)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid cart activity payload")

    email = (data.email or "").strip()
    phone = (data.phone or "").strip()

    # Nothing to reach them by — accept the request (this endpoint must
    # never make a typing customer's checkout look broken) but write
    # nothing. `recorded: false` is the honest answer.
    if not email and not phone:
        return JSONResponse(content={"recorded": False})

    if email and not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Invalid email")
    if phone:
        normalized_phone = normalize_phone(phone)
        if not normalized_phone or not (8 <= len(normalized_phone) <= 15):
            raise HTTPException(status_code=422, detail="Invalid phone")

    # Cap rather than reject: an oversized cart is far likelier to be an
    # abusive payload than a real shopper, and dropping the tail keeps the
    # lead instead of losing it.
    if data.cartItems and len(data.cartItems) > MAX_CART_ITEMS:
        data.cartItems = data.cartItems[:MAX_CART_ITEMS]

    try:
        stored = await AbandonedCartService().record(
            data,
            ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except Exception as exc:
        # Capture is a marketing nicety layered onto a live checkout — it
        # must never surface an error to someone mid-purchase.
        if logger:
            logger.warning(f"Cart activity capture failed: {exc}")
        return JSONResponse(content={"recorded": False})

    return JSONResponse(content={"recorded": stored is not None})
