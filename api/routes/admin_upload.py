"""Admin media upload to R2."""
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from typing import List
from api.bootstrap import R2Service, UploadValidationError, logger, require_admin, require_scope_email, validate_upload
from src.services.media_resize import recompress_upload

router = APIRouter()


@router.post("/api/admin/upload")
async def admin_upload(
    files: List[UploadFile] = File(...),
    folder: str = Form("products"),
    email: str = Depends(require_scope_email("media", "upload")),
):
    """Upload one or more media files to the Cloudflare R2 'chokmoki' bucket."""
    if R2Service is None:
        raise HTTPException(status_code=500, detail="R2 storage not initialized")

    safe_folder = "".join(c for c in folder if c.isalnum() or c in ("-", "_")) or "products"
    service = R2Service()
    urls: List[str] = []
    try:
        for upload in files:
            content = await upload.read()
            try:
                extension, content_type = validate_upload(
                    content, upload.filename or "upload.bin"
                )
            except UploadValidationError as ve:
                raise HTTPException(status_code=400, detail=str(ve)) from ve
            content, content_type = recompress_upload(content, content_type)
            if content_type == "image/webp":
                extension = "webp"
            url = await service.upload_file(
                content,
                extension=extension,
                content_type=content_type,
                folder=safe_folder,
            )
            urls.append(url)
    except HTTPException:
        raise
    except Exception as e:
        if logger:
            logger.error(f"Admin upload failed: {e}")
        raise HTTPException(status_code=500) from e

    return {"urls": urls}
