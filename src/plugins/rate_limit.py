from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.config import settings
from src.database.redis_connection import redis_client
from src.plugins.rate_limit_config import (
    ResolvedRateLimit,
    load_rate_limit_rules,
    match_rules,
    resolve_rate_limits,
)
from src.security.client_ip import get_client_ip, should_fail_closed_for_path
from src.plugins.token_bucket import TokenBucketLimiter


def _extract_admin_email(request: Request) -> Optional[str]:
    authorization = request.headers.get("Authorization") or ""
    token: Optional[str] = None
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        token = request.cookies.get("chokmoki_admin_access")

    if not token:
        return None

    try:
        from src.services.admin_auth_service import AdminAuthService

        payload = AdminAuthService()._decode_token(token)
        if payload and payload.get("type") == "admin_access":
            return payload.get("sub")
    except Exception:
        return None
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self._limiter = TokenBucketLimiter()
        self._rules = load_rate_limit_rules()

    def reload_rules(self) -> None:
        self._rules = load_rate_limit_rules()

    async def dispatch(self, request: Request, call_next: Callable):
        # Header/IP debug logging runs even when rate limiting is disabled.
        self._log_client_ip(request, request.url.path, get_client_ip(request))

        if not settings.rate_limit_enabled:
            return await call_next(request)

        path = request.url.path
        method = request.method.upper()

        # Only the rate-limit CHECK itself (below) is wrapped in try/except —
        # call_next(request), which runs the actual route handler, is
        # deliberately called OUTSIDE this block (see after it). Previously
        # call_next was inside the try too, so ANY unhandled exception
        # anywhere in the real request handling — not just a rate-limiter/
        # Redis failure — got caught here and relabeled as a generic,
        # UNLOGGED "Rate limit service unavailable" 503. That silently
        # masked a real bug (a Mongo unique-index collision on brand-new
        # customer signups, see src/services/user_service.py's
        # partialFilterExpression fix) behind a meaningless error with zero
        # trace in the logs, for as long as it went unnoticed.
        try:
            operation, body_fields = await self._extract_cqrs_context(request, path, method)
            admin_email = _extract_admin_email(request)
            ip = get_client_ip(request)

            if path == "/api/products" and method == "GET":
                if not request.query_params.get("search"):
                    checks: List[Tuple[str, Any]] = []
                else:
                    checks = self._build_checks(
                        method, path, operation, ip, admin_email, body_fields
                    )
            else:
                checks = self._build_checks(
                    method, path, operation, ip, admin_email, body_fields
                )

            if checks:
                redis = await redis_client.get_client()
                _ = redis  # ensure connectivity before token checks
                result = await self._limiter.check_all(checks)
                if not result.allowed:
                    retry_seconds = max(1, int(result.retry_after_ms / 1000))
                    return self._rate_limit_response(retry_seconds)

        except Exception:
            if should_fail_closed_for_path(path):
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={
                        "error": "Rate limit service unavailable",
                        "message": "Request blocked because rate limiting could not be verified",
                    },
                )
            # Fail open: the rate-limit check itself is broken, but that
            # must not block the actual request — fall through to call_next.

        return await call_next(request)

    def _log_client_ip(self, request: Request, path: str, resolved_ip: str) -> None:
        """Temporary debug: dump headers + resolved client IP for auth paths.

        Toggle with LOG_CLIENT_IP_HEADERS. Only logs auth-sensitive paths to
        avoid flooding stdout. Remove once real client IP is confirmed.
        """
        if not settings.log_client_ip_headers:
            return
        if not (path.startswith("/api/admin") or path == "/api/orders"):
            return
        try:
            peer = request.client.host if request.client else None
            headers = {k: v for k, v in request.headers.items()}
            print(
                f"[client-ip] path={path} resolved_ip={resolved_ip} "
                f"peer={peer} x-forwarded-for={headers.get('x-forwarded-for')!r} "
                f"x-real-ip={headers.get('x-real-ip')!r}",
                flush=True,
            )
            print(f"[client-ip] all-headers={headers}", flush=True)
        except Exception:
            pass

    def _build_checks(
        self,
        method: str,
        path: str,
        operation: Optional[str],
        ip: str,
        admin_email: Optional[str],
        body_fields: Dict[str, Any],
    ) -> List[Tuple[str, Any]]:
        resolved: List[ResolvedRateLimit] = resolve_rate_limits(
            self._rules,
            method=method,
            path=path,
            operation=operation,
            ip=ip,
            admin_email=admin_email,
            body_fields=body_fields,
        )
        return [(item.redis_key, item.bucket) for item in resolved]

    def _rate_limit_response(self, retry_after: int) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "Rate limit exceeded",
                "message": "Too many requests. Please try again later.",
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    async def _extract_cqrs_context(
        self, request: Request, path: str, method: str
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        if method not in {"POST", "PUT", "PATCH"}:
            return None, {}

        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" not in content_type and path != "/":
            return None, {}

        if path != "/" and not path.startswith("/api/"):
            return None, {}

        # Peeking the body and rewiring request._receive to replay it is the
        # one genuinely risky thing this middleware does — real-world
        # traffic (HTTP/2 multiplexed browser clients through a proxy) has
        # shown it can occasionally corrupt the body FastAPI's own handler
        # reads afterwards, surfacing as "There was an error parsing the
        # body" on requests that never even needed a body-derived rate
        # limit (e.g. /api/auth/otp/verify, which only has an ip-scope
        # fallback bucket). So only pay that cost on paths where some rule
        # actually has an email-scope bucket that needs a body field — for
        # everything else, skip entirely and let the body pass through
        # completely untouched. The CQRS gateway ("/") still always reads
        # the body since `operation` only exists inside it.
        if path != "/":
            matched = match_rules(self._rules, method=method, path=path, operation=None)
            needs_body = any(bucket.scope == "email" for rule in matched for bucket in rule.buckets)
            if not needs_body:
                return None, {}

        try:
            body_bytes = await request.body()
            if not body_bytes:
                return None, {}

            data = json.loads(body_bytes)
            operation = data.get("operation") if path == "/" else None
            params = data.get("params", {}) if path == "/" else data

            fields: Dict[str, Any] = {}
            if isinstance(params, dict):
                fields.update(params)
            if isinstance(data, dict):
                for key in ("email", "userEmail", "user_email"):
                    if key in data:
                        fields[key] = data[key]

            async def receive() -> dict:
                return {"type": "http.request", "body": body_bytes}

            request._receive = receive
            return operation, fields
        except Exception:
            return None, {}
