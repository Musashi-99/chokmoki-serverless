from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, Request

from src.config import settings
from src.models.admin_auth import AdminPrincipal
from src.services.admin_auth_service import AdminAuthService

ACCESS_COOKIE = "chokmoki_admin_access"
REFRESH_COOKIE = "chokmoki_admin_refresh"
CSRF_COOKIE = "chokmoki_csrf"
CSRF_HEADER = "X-CSRF-Token"


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return token or None


async def resolve_admin_principal(
    request: Request,
    authorization: Optional[str] = Header(None),
    access_cookie: Optional[str] = Cookie(None, alias=ACCESS_COOKIE),
) -> AdminPrincipal:
    auth_service = AdminAuthService()
    bearer = _extract_bearer(authorization)
    token = bearer or access_cookie
    if not token:
        raise HTTPException(status_code=401, detail="Missing admin credentials")

    principal = await auth_service.verify_access_token(token)
    if not principal:
        raise HTTPException(status_code=401, detail="Invalid or expired admin session")

    request.state.admin_email = principal.email
    request.state.admin_principal = principal
    request.state.admin_auth_via_cookie = bool(access_cookie and not bearer)
    return principal


async def require_admin(
    request: Request,
    principal: AdminPrincipal = Depends(resolve_admin_principal),
) -> str:
    if settings.csrf_enabled and getattr(request.state, "admin_auth_via_cookie", False):
        await _enforce_csrf(request)
    return principal.email


async def _enforce_csrf(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return

    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    csrf_header = request.headers.get(CSRF_HEADER)
    if not csrf_cookie or not csrf_header:
        raise HTTPException(status_code=403, detail="CSRF token required")
    if not secrets.compare_digest(csrf_cookie, csrf_header):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


# Scope/ABAC enforcement (require_scope) lives in src/security/abac.py, which
# imports resolve_admin_principal and _enforce_csrf from this module — kept
# here rather than duplicated so CSRF/session resolution has exactly one
# implementation. The old require_permission (role.has_permission-based,
# never wired into any route) has been fully replaced by Vakt-backed
# require_scope.

