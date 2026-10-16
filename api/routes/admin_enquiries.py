"""Admin enquiry management — list, status update, CSV export.

Gated by its own `enquiries:read` / `enquiries:write` scopes. Enquiries have
no region, so there is no regional scoping here (unlike pre-orders)."""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from api.bootstrap import AdminPrincipal, require_scope
from api.json_utils import JSONEncoder
from src.models.enquiry import ENQUIRY_STATUSES
from src.services.enquiry_service import EnquiryService, csv_safe, public_row

router = APIRouter()

_CSV_COLUMNS = [
    "name", "email", "phone", "product_name", "quantity", "size",
    "notify_via", "message", "status", "created_at",
]


@router.get("/api/admin/enquiries")
async def admin_list_enquiries(
    skip: int = 0,
    limit: int = 50,
    status: Optional[str] = None,
    search: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    principal: AdminPrincipal = Depends(require_scope("enquiries", "read")),
):
    service = EnquiryService()
    try:
        docs = await service.list(skip=skip, limit=limit, status=status, search=search,
                                  from_date=from_date, to_date=to_date)
        total = await service.count(status=status, search=search, from_date=from_date, to_date=to_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    body = json.dumps({"data": [public_row(d) for d in docs], "count": total}, cls=JSONEncoder)
    return JSONResponse(content=json.loads(body))


@router.patch("/api/admin/enquiries/{enquiry_id}/status")
async def admin_update_enquiry_status(
    enquiry_id: str,
    payload: Dict[str, Any],
    principal: AdminPrincipal = Depends(require_scope("enquiries", "write")),
):
    status = (payload or {}).get("status")
    if status not in ENQUIRY_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(ENQUIRY_STATUSES)}")
    updated = await EnquiryService().update_status(enquiry_id, status)
    if not updated:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    return JSONResponse(content=json.loads(json.dumps(public_row(updated), cls=JSONEncoder)))


@router.get("/api/admin/enquiries/export.csv")
async def admin_export_enquiries_csv(
    status: Optional[str] = None,
    search: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    principal: AdminPrincipal = Depends(require_scope("enquiries", "read")),
):
    docs = await EnquiryService().list(skip=0, limit=10_000, status=status, search=search,
                                       from_date=from_date, to_date=to_date)

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(_CSV_COLUMNS)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for doc in docs:
            row = public_row(doc)
            created = row.get("created_at")
            writer.writerow([
                csv_safe(row.get("name")), csv_safe(row.get("email")), csv_safe(row.get("phone")),
                csv_safe(row.get("product_name")) or "General enquiry",
                row.get("quantity"), csv_safe(row.get("size")),
                "|".join(row.get("notify_via") or []), csv_safe(row.get("message")),
                row.get("status"), created.isoformat() if created else "",
            ])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="enquiries.csv"'},
    )
