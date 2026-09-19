"""Route wiring tests for GET /api/admin/abandoned-carts."""
from __future__ import annotations

import importlib
import os
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _stub_external_modules() -> None:
    telegram = types.ModuleType("telegram")
    telegram.Bot = object
    telegram_error = types.ModuleType("telegram.error")
    telegram_error.TelegramError = Exception
    telegram_error.NetworkError = Exception
    telegram_error.RetryAfter = Exception
    telegram_error.TimedOut = Exception
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *args, **kwargs: object()
    botocore = types.ModuleType("botocore")
    botocore_client = types.ModuleType("botocore.client")
    botocore_client.Config = object
    botocore.client = botocore_client
    sys.modules["telegram"] = telegram
    sys.modules["telegram.error"] = telegram_error
    sys.modules["boto3"] = boto3
    sys.modules["botocore"] = botocore
    sys.modules["botocore.client"] = botocore_client


@pytest.fixture
def api_module(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    _stub_external_modules()
    if "api.index" in sys.modules:
        del sys.modules["api.index"]
    return importlib.import_module("api.index")


def _fake_principal(scopes=frozenset({"*"})):
    from src.models.admin_auth import AdminPrincipal

    return AdminPrincipal(
        email="admin@test.com",
        role="super_admin",
        session_id="test-session",
        jti="test-jti",
        scopes=scopes,
        regions=frozenset(),
        is_root=True,
    )


class TestRouteRegistered:
    def test_route_registered(self, api_module):
        paths = {route.path for route in api_module.app.routes}
        assert "/api/admin/abandoned-carts" in paths


class TestRequiresAdmin:
    def test_unauthenticated_request_is_401(self, api_module):
        client = TestClient(api_module.app, raise_server_exceptions=True)
        response = client.get("/api/admin/abandoned-carts")
        assert response.status_code == 401


class TestListShapeAndDefaults:
    def test_returns_data_and_count_with_default_page_size_30(self, api_module):
        from src.plugins.admin_deps import resolve_admin_principal

        api_module.app.dependency_overrides[resolve_admin_principal] = lambda: _fake_principal()
        try:
            with (
                patch(
                    "api.routes.admin_abandoned_carts.AbandonedCartService.list",
                    new_callable=AsyncMock,
                ) as mock_list,
                patch(
                    "api.routes.admin_abandoned_carts.AbandonedCartService.count",
                    new_callable=AsyncMock,
                ) as mock_count,
            ):
                mock_list.return_value = []
                mock_count.return_value = 0
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.get("/api/admin/abandoned-carts")
        finally:
            api_module.app.dependency_overrides.pop(resolve_admin_principal, None)

        assert response.status_code == 200
        body = response.json()
        assert body == {"data": [], "count": 0}
        # default limit=30 forwarded to the service
        assert mock_list.call_args.kwargs["limit"] == 30

    def test_search_and_filters_are_forwarded_to_the_service(self, api_module):
        from src.plugins.admin_deps import resolve_admin_principal

        api_module.app.dependency_overrides[resolve_admin_principal] = lambda: _fake_principal()
        try:
            with (
                patch(
                    "api.routes.admin_abandoned_carts.AbandonedCartService.list",
                    new_callable=AsyncMock,
                ) as mock_list,
                patch(
                    "api.routes.admin_abandoned_carts.AbandonedCartService.count",
                    new_callable=AsyncMock,
                ) as mock_count,
            ):
                mock_list.return_value = []
                mock_count.return_value = 0
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.get(
                    "/api/admin/abandoned-carts",
                    params={
                        "skip": 30, "limit": 30, "search": "9876543210",
                        "country": "AU", "converted": "false",
                    },
                )
        finally:
            api_module.app.dependency_overrides.pop(resolve_admin_principal, None)

        assert response.status_code == 200
        _, kwargs = mock_list.call_args
        assert kwargs["skip"] == 30
        assert kwargs["search"] == "9876543210"
        assert kwargs["country"] == "AU"
        assert kwargs["converted"] is False
