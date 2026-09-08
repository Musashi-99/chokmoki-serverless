"""Root-only admin-user management: invite, list, sessions, QR/TOTP
regenerate, deactivate/reactivate. Enrollment endpoints are public (no
session yet) but token-gated."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.bootstrap import (
    AdminUserCreate,  # noqa: F401 (kept importable for future request-model reuse)
    AdminUserError,
    AdminUserPublic,
    AdminUserService,
    EVENT_ADMIN_MUTATION,
    EmailService,
    RootAccountImmutableError,
    publish_alert,
    require_scope,
    settings,
)
from src.models.admin_auth import AdminPrincipal
from src.services.admin_session_service import AdminSessionService

router = APIRouter()


def _service() -> "AdminUserService":
    if AdminUserService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return AdminUserService()


async def _notify(actor_email: str, action: str, metadata: dict) -> None:
    if publish_alert is None or EVENT_ADMIN_MUTATION is None:
        return
    try:
        await publish_alert(
            EVENT_ADMIN_MUTATION,
            {"actor_email": actor_email, "resource": "admins", "action": action, **metadata},
        )
    except Exception:
        pass


async def _send_enrollment_email(email: str, name: str, token: str) -> None:
    if EmailService is None:
        return
    link = f"{settings.frontend_url}/admin/enroll/{token}" if settings else f"/admin/enroll/{token}"
    subject = "You've been invited as a Chokmoki admin"
    html = (
        f"<p>Hi {name or email},</p>"
        f"<p>You've been invited to the Chokmoki admin panel. "
        f"Click the link below to set up your authenticator app and finish enrolling. "
        f"This link expires in 30 minutes.</p>"
        f'<p><a href="{link}">{link}</a></p>'
    )
    await EmailService().send(email, subject, html)


class InviteAdminRequest(BaseModel):
    email: str
    name: str
    role: str
    scopes: List[str] = []
    region: Optional[str] = None


class ConfirmEnrollRequest(BaseModel):
    code: str


class SessionRevokeRequest(BaseModel):
    session_id: str


@router.post("/api/admin/admins")
async def invite_admin(
    payload: InviteAdminRequest,
    principal: AdminPrincipal = Depends(require_scope("admins", "write")),
):
    service = _service()
    try:
        doc, token = await service.create_invite(
            email=payload.email,
            name=payload.name,
            role=payload.role,
            scopes=payload.scopes,
            region=payload.region,
            created_by=principal.email,
        )
    except AdminUserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await _send_enrollment_email(payload.email, payload.name, token)
    await _notify(principal.email, "invite_admin", {"invited_email": payload.email, "region": payload.region})
    return AdminUserPublic.from_doc(doc)


@router.get("/api/admin/admins")
async def list_admins(principal: AdminPrincipal = Depends(require_scope("admins", "read"))):
    service = _service()
    docs = await service.list_admins()
    return [AdminUserPublic.from_doc(d) for d in docs]


@router.get("/api/admin/admins/{admin_id}")
async def get_admin(admin_id: str, principal: AdminPrincipal = Depends(require_scope("admins", "read"))):
    service = _service()
    doc = await service.get_by_id(admin_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Admin not found")
    return AdminUserPublic.from_doc(doc)


@router.get("/api/admin/admins/{admin_id}/sessions")
async def list_admin_sessions(admin_id: str, principal: AdminPrincipal = Depends(require_scope("admins", "read"))):
    service = _service()
    doc = await service.get_by_id(admin_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Admin not found")
    sessions = await AdminSessionService().list_user_sessions(doc["email"])
    return {"sessions": sessions}


@router.post("/api/admin/admins/{admin_id}/sessions/revoke")
async def revoke_admin_session(
    admin_id: str,
    payload: SessionRevokeRequest,
    principal: AdminPrincipal = Depends(require_scope("admins", "write")),
):
    service = _service()
    doc = await service.get_by_id(admin_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Admin not found")
    await AdminSessionService().revoke_session(payload.session_id)
    await _notify(principal.email, "revoke_session", {"target_email": doc["email"]})
    return {"success": True}


@router.post("/api/admin/admins/{admin_id}/sessions/revoke-all")
async def revoke_all_admin_sessions(
    admin_id: str, principal: AdminPrincipal = Depends(require_scope("admins", "write"))
):
    service = _service()
    doc = await service.get_by_id(admin_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Admin not found")
    count = await AdminSessionService().revoke_all_user_sessions(doc["email"])
    await _notify(principal.email, "revoke_all_sessions", {"target_email": doc["email"], "count": count})
    return {"success": True, "revoked": count}


@router.post("/api/admin/admins/{admin_id}/qr/regenerate")
async def regenerate_admin_qr(admin_id: str, principal: AdminPrincipal = Depends(require_scope("admins", "write"))):
    service = _service()
    try:
        token = await service.regenerate_totp(admin_id, actor_email=principal.email)
    except RootAccountImmutableError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AdminUserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    doc = await service.get_by_id(admin_id)
    await _send_enrollment_email(doc["email"], doc.get("name", ""), token)
    await _notify(principal.email, "regenerate_totp", {"target_email": doc["email"]})
    return {"success": True}


@router.post("/api/admin/admins/{admin_id}/deactivate")
async def deactivate_admin(admin_id: str, principal: AdminPrincipal = Depends(require_scope("admins", "write"))):
    service = _service()
    try:
        doc = await service.deactivate(admin_id, actor_email=principal.email)
    except RootAccountImmutableError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AdminUserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await AdminSessionService().revoke_all_user_sessions(doc["email"])
    await _notify(principal.email, "deactivate_admin", {"target_email": doc["email"]})
    return AdminUserPublic.from_doc(doc)


@router.post("/api/admin/admins/{admin_id}/reactivate")
async def reactivate_admin(admin_id: str, principal: AdminPrincipal = Depends(require_scope("admins", "write"))):
    service = _service()
    try:
        doc = await service.reactivate(admin_id, actor_email=principal.email)
    except RootAccountImmutableError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AdminUserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await _notify(principal.email, "reactivate_admin", {"target_email": doc["email"]})
    return AdminUserPublic.from_doc(doc)


@router.get("/api/admin/enroll/{token}")
async def get_enrollment_info(token: str):
    """Public — validates the invite token itself (single-use, short TTL)."""
    service = _service()
    info = await service.get_enrollment_info(token)
    if not info:
        raise HTTPException(status_code=404, detail="Invite link is invalid or has expired")
    return info


@router.post("/api/admin/enroll/{token}/confirm")
async def confirm_enrollment(token: str, payload: ConfirmEnrollRequest):
    """Public — verifies the first TOTP code, activates the account, and
    consumes the token. Does not auto-login; the admin proceeds to
    /admin to log in with email + TOTP."""
    service = _service()
    ok = await service.confirm_enrollment(token, payload.code)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid code or expired invite")
    return {"success": True}
