"""Code-defined ABAC layer (Vakt) for admin authorization.

Replaces the previously-dead `require_permission` dependency
(src/plugins/admin_deps.py) with `require_scope`, backed by Vakt policies
generated from the `AdminPermission` taxonomy (src/models/admin_rbac.py).

Policies are built once at import time from `MemoryStorage` + `RulesChecker`
— no DB-backed policy editor in this phase (see docs/plans). Vakt denies by
default when no policy matches an Inquiry (verified in
tests/test_abac_deny_by_default.py), so an empty/unmapped scope always fails
closed rather than open.

Region is deliberately never referenced by any policy here — it's carried
on `AdminPrincipal`/the Inquiry subject only so Telegram routing
(src/alerts/handlers.py) can read it. It must never gate access to any
resource/action in this phase.
"""
from __future__ import annotations

from typing import Callable, FrozenSet, Optional

import vakt
from fastapi import Depends, HTTPException, Request
from vakt.rules.base import Rule
from vakt.rules.operator import Eq

from src.config import settings
from src.models.admin_auth import AdminPrincipal
from src.models.admin_rbac import AdminPermission
from src.plugins.admin_deps import _enforce_csrf, resolve_admin_principal

# Permissions whose resource is "admins" get a hard root-only gate instead of
# the generic scope-based policy — see build_policies() below.
_ROOT_ONLY_PERMISSIONS = {
    AdminPermission.ADMINS_READ.value,
    AdminPermission.ADMINS_WRITE.value,
}


class HasScopeRule(Rule):
    """Satisfied when `permission` is present in the subject's scope list,
    or the subject holds the wildcard "*" scope (root and legacy env-admin
    tokens)."""

    def __init__(self, permission: str) -> None:
        self.permission = permission

    def satisfied(self, what, inquiry=None) -> bool:
        scopes = what or []
        return self.permission in scopes or "*" in scopes


class IsRootRule(Rule):
    """Satisfied only when the subject's `is_root` attribute is exactly True."""

    def satisfied(self, what, inquiry=None) -> bool:
        return what is True


def _build_policies() -> list[vakt.Policy]:
    policies: list[vakt.Policy] = []

    for permission in AdminPermission:
        value = permission.value
        if value in _ROOT_ONLY_PERMISSIONS:
            continue
        if ":" not in value:
            # e.g. AdminPermission.ACCESS ("admin:access") — not a
            # resource:action pair used by require_scope; skip.
            continue
        resource, action = value.split(":", 1)
        policies.append(
            vakt.Policy(
                uid=f"perm:{value}",
                effect=vakt.ALLOW_ACCESS,
                subjects=[{"scopes": HasScopeRule(value)}],
                resources=[Eq(resource)],
                actions=[Eq(action)],
            )
        )

    for value in _ROOT_ONLY_PERMISSIONS:
        resource, action = value.split(":", 1)
        policies.append(
            vakt.Policy(
                uid=f"perm:{value}:root-only",
                effect=vakt.ALLOW_ACCESS,
                subjects=[{"scopes": HasScopeRule(value), "is_root": IsRootRule()}],
                resources=[Eq(resource)],
                actions=[Eq(action)],
            )
        )

    return policies


_storage = vakt.MemoryStorage()
for _policy in _build_policies():
    _storage.add(_policy)

guard = vakt.Guard(_storage, vakt.RulesChecker())


def build_inquiry(principal: AdminPrincipal, resource: str, action: str) -> vakt.Inquiry:
    scopes: FrozenSet[str] = getattr(principal, "scopes", None) or frozenset()
    return vakt.Inquiry(
        resource=resource,
        action=action,
        subject={
            "email": principal.email,
            "role": principal.role,
            "scopes": list(scopes),
            "regions": list(getattr(principal, "regions", None) or []),
            "is_root": bool(getattr(principal, "is_root", False)),
        },
    )


def is_allowed(principal: AdminPrincipal, resource: str, action: str) -> bool:
    return guard.is_allowed(build_inquiry(principal, resource, action))


def require_scope(resource: str, action: str) -> Callable:
    """FastAPI dependency: resolves the admin principal (same CSRF/session
    flow as require_admin), then denies with 403 unless a Vakt policy
    explicitly allows resource:action for that principal's scopes.
    """

    async def _dependency(
        request: Request,
        principal: AdminPrincipal = Depends(resolve_admin_principal),
    ) -> AdminPrincipal:
        if settings.csrf_enabled and getattr(request.state, "admin_auth_via_cookie", False):
            await _enforce_csrf(request)
        if not is_allowed(principal, resource, action):
            raise HTTPException(
                status_code=403, detail=f"Missing scope: {resource}:{action}"
            )
        request.state.admin_email = principal.email
        request.state.admin_principal = principal
        return principal

    return _dependency


def require_scope_email(resource: str, action: str) -> Callable:
    """Same enforcement as require_scope, but yields just `principal.email`
    (a str) — a drop-in replacement for the existing `email: str =
    Depends(require_admin)` parameter shape used across the pre-existing
    admin routers, so wiring in scope checks doesn't require touching every
    route body."""
    scoped = require_scope(resource, action)

    async def _dependency(principal: AdminPrincipal = Depends(scoped)) -> str:
        return principal.email

    return _dependency
