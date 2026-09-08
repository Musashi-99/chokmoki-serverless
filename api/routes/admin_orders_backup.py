"""Raw orders + order_logs data source for the unified export ZIP.

Orders no longer have their own export/import UI or restore endpoint — they're
bundled into the single "Export everything" ZIP built client-side by
aurum-editorial/src/lib/contentBundle.ts (orders/orders.json + orders/order_logs.json)
and restored through POST /api/admin/import alongside the 17 config sections.
This GET endpoint just supplies the raw documents the ZIP builder embeds; it is
not exposed as a standalone admin button."""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from api.bootstrap import db, export_orders_backup, require_admin, require_scope_email

router = APIRouter()


@router.get("/api/admin/orders/export")
async def admin_export_orders(email: str = Depends(require_scope_email("orders", "read"))):
    """Dump every order + order log as JSON, for embedding into the unified export ZIP."""
    if db is None or export_orders_backup is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    database = await db.get_database()
    payload = await export_orders_backup(database)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        content=json.dumps(payload),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="chokmoki-orders-backup-{stamp}.json"'},
    )
