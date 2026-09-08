"""Admin inbox: contact submissions + newsletter subscriptions."""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from typing import Any, Dict
from api.bootstrap import InboxService, require_admin, require_scope_email
from api.json_utils import _json_response_content

router = APIRouter()


@router.get("/api/admin/inbox")
async def admin_get_inbox(
    skip: int = 0,
    limit: int = 100,
    contacts_skip: int | None = None,
    contacts_limit: int | None = None,
    newsletter_skip: int | None = None,
    newsletter_limit: int | None = None,
    email: str = Depends(require_scope_email("inbox", "read")),
):
    if InboxService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    service = InboxService()
    cs = contacts_skip if contacts_skip is not None else skip
    cl = contacts_limit if contacts_limit is not None else limit
    ns = newsletter_skip if newsletter_skip is not None else skip
    nl = newsletter_limit if newsletter_limit is not None else limit
    contacts = await service.list_contacts(skip=cs, limit=cl)
    newsletter = await service.list_newsletter(skip=ns, limit=nl)
    return JSONResponse(content=_json_response_content({
        "contacts": contacts,
        "contacts_count": await service.count_contacts(),
        "contacts_unread": await service.count_contacts(unread_only=True),
        "newsletter": newsletter,
        "newsletter_count": await service.count_newsletter(),
        "newsletter_unread": await service.count_newsletter(unread_only=True),
    }))


@router.patch("/api/admin/inbox/contacts/{submission_id}")
async def admin_patch_contact(
    submission_id: str, payload: Dict[str, Any], email: str = Depends(require_scope_email("inbox", "read"))
):
    if InboxService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    read = payload.get("read", True)
    ok = await InboxService().mark_contact_read(submission_id, bool(read))
    if not ok:
        raise HTTPException(status_code=404, detail="Contact submission not found")
    return {"success": True}


@router.delete("/api/admin/inbox/contacts/{submission_id}")
async def admin_delete_contact(submission_id: str, email: str = Depends(require_scope_email("inbox", "read"))):
    if InboxService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    if not await InboxService().delete_contact(submission_id):
        raise HTTPException(status_code=404, detail="Contact submission not found")
    return {"success": True}


@router.patch("/api/admin/inbox/newsletter/{sub_id}")
async def admin_patch_newsletter(
    sub_id: str, payload: Dict[str, Any], email: str = Depends(require_scope_email("inbox", "read"))
):
    if InboxService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    read = payload.get("read", True)
    ok = await InboxService().mark_newsletter_read(sub_id, bool(read))
    if not ok:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"success": True}


@router.delete("/api/admin/inbox/newsletter/{sub_id}")
async def admin_delete_newsletter(sub_id: str, email: str = Depends(require_scope_email("inbox", "read"))):
    if InboxService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    if not await InboxService().delete_newsletter(sub_id):
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"success": True}
