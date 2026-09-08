"""Admin fraud review queue."""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from typing import Any, Dict
from api.bootstrap import FraudReviewService, require_admin, require_scope_email
from api.json_utils import _json_response_content

router = APIRouter()


@router.get("/api/admin/fraud/reviews")
async def admin_list_fraud_reviews(
    skip: int = 0,
    limit: int = 50,
    email: str = Depends(require_scope_email("orders", "write")),
):
    if FraudReviewService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    reviews = await FraudReviewService().list_pending(skip=skip, limit=limit)
    return JSONResponse(content=_json_response_content({"data": reviews, "count": len(reviews)}))


@router.post("/api/admin/fraud/reviews/{review_id}/resolve")
async def admin_resolve_fraud_review(
    review_id: str,
    payload: Dict[str, Any],
    email: str = Depends(require_scope_email("orders", "write")),
):
    if FraudReviewService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    status = (payload.get("status") or "").strip().lower()
    note = (payload.get("note") or "").strip()
    try:
        ok = await FraudReviewService().resolve(review_id, status=status, note=note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="Review item not found")
    return {"success": True, "status": status}
