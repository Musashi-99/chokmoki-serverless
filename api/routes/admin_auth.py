"""Admin login/refresh/logout/me (cookie session auth)."""
from fastapi import APIRouter, HTTPException, Request, Depends, Cookie
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from api.bootstrap import AccountLockedError, AdminAuthService, AdminRole, MFACodeRequired, clear_auth_cookies, get_client_ip, require_admin, set_auth_cookies, settings

router = APIRouter()


class AdminLoginRequest(BaseModel):
    email: str
    password: Optional[str] = None
    totp_code: Optional[str] = None


@router.get("/api/admin/login/mode")
async def admin_login_mode(email: str):
    """Precheck for the frontend login form: tells it whether to render a
    password field. Never distinguishes 'no such account' from 'account
    exists but is inactive' — both come back as 'unknown'."""
    if AdminAuthService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    mode = await AdminAuthService().resolve_login_mode(email)
    return {"mode": mode}


@router.post("/api/admin/login")
async def admin_login(payload: AdminLoginRequest, request: Request):
    """Authenticate admin; sets httpOnly session cookies. Root (the
    env-configured account) logs in with email+password(+TOTP) as before;
    every invited admin logs in passwordless (email+TOTP only)."""
    if AdminAuthService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    client_ip = get_client_ip(request) if get_client_ip else "unknown"
    auth_service = AdminAuthService()

    try:
        if payload.password is not None:
            result = await auth_service.authenticate(
                payload.email,
                payload.password,
                payload.totp_code,
                client_ip=client_ip,
            )
        else:
            result = await auth_service.authenticate_passwordless(
                payload.email,
                payload.totp_code,
                client_ip=client_ip,
            )
    except MFACodeRequired:
        raise HTTPException(status_code=401, detail="MFA code required")
    except AccountLockedError as exc:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    if not result:
        detail = "Invalid email or password" if payload.password is not None else "Invalid email or code"
        raise HTTPException(status_code=401, detail=detail)

    body = {
        "email": result.email,
        "role": result.role,
        "expires_in": result.tokens.expires_in,
        "mfa_enabled": settings.admin_mfa_enabled if settings else False,
        "scopes": sorted(result.scopes) if result.scopes else [],
        "regions": sorted(result.regions) if result.regions else [],
        "is_root": result.is_root,
    }
    if settings and settings.admin_legacy_bearer_enabled:
        body["token"] = result.tokens.access_token

    response = JSONResponse(content=body)
    if set_auth_cookies:
        set_auth_cookies(response, result.tokens)
    return response


@router.post("/api/admin/refresh")
async def admin_refresh(
    refresh_cookie: Optional[str] = Cookie(None, alias="chokmoki_admin_refresh"),
):
    """Rotate refresh token and issue a new access token."""
    if AdminAuthService is None or not refresh_cookie:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    tokens = await AdminAuthService().refresh(refresh_cookie)
    if not tokens:
        response = JSONResponse(status_code=401, content={"detail": "Invalid refresh token"})
        if clear_auth_cookies:
            clear_auth_cookies(response)
        return response

    body = {"expires_in": tokens.expires_in}
    if settings and settings.admin_legacy_bearer_enabled:
        body["token"] = tokens.access_token

    response = JSONResponse(content=body)
    if set_auth_cookies:
        set_auth_cookies(response, tokens)
    return response


@router.post("/api/admin/logout")
async def admin_logout(
    request: Request,
    refresh_cookie: Optional[str] = Cookie(None, alias="chokmoki_admin_refresh"),
):
    """Invalidate the current admin session."""
    if AdminAuthService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    principal = getattr(request.state, "admin_principal", None)
    auth_service = AdminAuthService()

    if principal is None:
        authorization = request.headers.get("Authorization")
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
        access_cookie = request.cookies.get("chokmoki_admin_access")
        verify_token = token or access_cookie
        if verify_token:
            principal = await auth_service.verify_access_token(verify_token)

    await auth_service.logout(principal, refresh_cookie)
    response = JSONResponse(content={"success": True})
    if clear_auth_cookies:
        clear_auth_cookies(response)
    return response


@router.get("/api/admin/me")
async def admin_me(request: Request, email: str = Depends(require_admin)):
    """Return the currently authenticated admin."""
    principal = getattr(request.state, "admin_principal", None)
    return {
        "email": email,
        "role": principal.role if principal else (AdminRole.SUPER_ADMIN.value if AdminRole else "super_admin"),
        "mfa_enabled": settings.admin_mfa_enabled if settings else False,
        "scopes": sorted(principal.scopes) if principal and principal.scopes else [],
        "regions": sorted(principal.regions) if principal and principal.regions else [],
        "is_root": bool(principal.is_root) if principal else False,
    }
