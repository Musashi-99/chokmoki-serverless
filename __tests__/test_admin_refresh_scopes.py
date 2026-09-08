"""Regression test: AdminAuthService.refresh() must re-resolve the actual
scopes/regions for the admin being refreshed, not default to a wildcard.
Previously _create_access_token() was called with no scopes argument at
all inside refresh(), and its (now-removed) default silently minted a "*"
(full-access) token for every admin on every refresh, regardless of their
real assigned scopes."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from jose import jwt

from src.config import settings
from src.services.admin_auth_service import AdminAuthService


@pytest.mark.asyncio
async def test_refresh_preserves_scoped_admins_actual_scopes():
    service = AdminAuthService()
    service.sessions.rotate_refresh_token = AsyncMock(
        return_value=("session-1", "regional@chokmoki.com", "regional_admin", "new-refresh-token")
    )
    service.admin_users.get_by_email = AsyncMock(
        return_value={
            "email": "regional@chokmoki.com",
            "status": "active",
            "scopes": ["orders:read", "inbox:read"],
            "regions": ["IN", "AU"],
        }
    )

    tokens = await service.refresh("some-refresh-token")

    assert tokens is not None
    payload = jwt.decode(tokens.access_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert payload["scopes"] == ["orders:read", "inbox:read"]
    assert set(payload["regions"]) == {"IN", "AU"}
    assert payload["is_root"] is False
    assert "*" not in payload["scopes"]


@pytest.mark.asyncio
async def test_refresh_grants_wildcard_only_to_actual_root():
    service = AdminAuthService()
    service.sessions.rotate_refresh_token = AsyncMock(
        return_value=("session-1", settings.admin_email, "super_admin", "new-refresh-token")
    )
    service.admin_users.get_by_email = AsyncMock(side_effect=AssertionError("must not query Mongo for root"))

    tokens = await service.refresh("some-refresh-token")

    assert tokens is not None
    payload = jwt.decode(tokens.access_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert payload["scopes"] == ["*"]
    assert payload["is_root"] is True


@pytest.mark.asyncio
async def test_refresh_rejects_deactivated_admin_and_revokes_session():
    service = AdminAuthService()
    service.sessions.rotate_refresh_token = AsyncMock(
        return_value=("session-1", "gone@chokmoki.com", "admin", "new-refresh-token")
    )
    service.admin_users.get_by_email = AsyncMock(
        return_value={"email": "gone@chokmoki.com", "status": "deactivated", "scopes": ["*"], "regions": []}
    )
    service.sessions.revoke_session = AsyncMock()

    tokens = await service.refresh("some-refresh-token")

    assert tokens is None
    service.sessions.revoke_session.assert_awaited_once_with("session-1")
