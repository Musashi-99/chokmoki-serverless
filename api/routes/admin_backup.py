"""Raw JSON config/orders backup — export and import as plain .json files
(distinct from the ZIP-based site-content bundle in admin_import.py). Two
independent pairs: config (everything except orders) and orders
(orders + order_logs only), each with wipe|merge restore modes."""
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import Response

from api.bootstrap import (
    db,
    cache,
    require_admin,
    require_scope_email,
    logger,
    settings,
    BackupParseError,
    BackupVersionError,
    export_config,
    export_orders,
    parse_config_backup,
    parse_orders_backup,
    import_backup,
    MAX_BACKUP_BYTES,
)

router = APIRouter()


def _filename(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"chokmoki-{prefix}-backup-{stamp}.json"


def _json_download(payload: dict, filename: str) -> Response:
    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _read_and_check_size(upload: UploadFile) -> bytes:
    raw = await upload.read()
    if len(raw) > MAX_BACKUP_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Backup exceeds maximum size of {MAX_BACKUP_BYTES // (1024 * 1024)}MB",
        )
    return raw


@router.get("/api/admin/export/config")
async def admin_export_config(email: str = Depends(require_scope_email("settings", "write"))):
    """Dump every content collection except orders/order_logs as one JSON file."""
    if db is None or export_config is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    database = await db.get_database()
    db_name = settings.mongodb_db_name if settings else database.name
    payload = await export_config(database, db_name)
    return _json_download(payload, _filename("config"))


@router.post("/api/admin/import/config")
async def admin_import_config(
    file: UploadFile = File(...),
    mode: Literal["wipe", "merge"] = Query("merge"),
    email: str = Depends(require_scope_email("settings", "write")),
):
    """Restore the config bundle. mode=wipe drops and reinserts each listed
    collection; mode=merge upserts by _id/slug, leaving everything else untouched."""
    if db is None or parse_config_backup is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    raw = await _read_and_check_size(file)

    try:
        collections = parse_config_backup(raw)
    except BackupVersionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except BackupParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    database = await db.get_database()
    try:
        result = await import_backup(database, collections, mode)
    except Exception as e:
        if logger:
            logger.error(f"Admin config import failed: {e}")
        raise HTTPException(status_code=500, detail="Restore failed") from e

    if cache is not None:
        await cache.delete_pattern("chokmoki:*")

    return {"mode": result.mode, "restored": result.restored, "skipped": result.skipped}


@router.get("/api/admin/export/orders")
async def admin_export_orders_json(email: str = Depends(require_scope_email("settings", "write"))):
    """Dump orders + order_logs only, as one JSON file."""
    if db is None or export_orders is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    database = await db.get_database()
    db_name = settings.mongodb_db_name if settings else database.name
    payload = await export_orders(database, db_name)
    return _json_download(payload, _filename("orders"))


@router.post("/api/admin/import/orders")
async def admin_import_orders_json(
    file: UploadFile = File(...),
    mode: Literal["wipe", "merge"] = Query("merge"),
    email: str = Depends(require_scope_email("settings", "write")),
):
    """Restore orders + order_logs only, independent of the config endpoints."""
    if db is None or parse_orders_backup is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    raw = await _read_and_check_size(file)

    try:
        collections = parse_orders_backup(raw)
    except BackupVersionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except BackupParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    database = await db.get_database()
    try:
        result = await import_backup(database, collections, mode)
    except Exception as e:
        if logger:
            logger.error(f"Admin orders import failed: {e}")
        raise HTTPException(status_code=500, detail="Restore failed") from e

    if cache is not None:
        await cache.delete_pattern("chokmoki:*")

    return {"mode": result.mode, "restored": result.restored, "skipped": result.skipped}
