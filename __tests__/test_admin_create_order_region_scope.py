"""Route-level tests for POST /api/admin/orders' new region enforcement
(_assert_order_country_in_scope, api/routes/admin_orders.py): a regional
admin may only create a manual order for one of their own assigned
region(s) — the create-time counterpart to _enforce_order_region, which
already guards reading/updating an EXISTING order."""
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
    module = importlib.import_module("api.index")
    from src.config import settings as _settings

    monkeypatch.setattr(_settings, "rate_limit_enabled", False)
    yield module

    from src.database.redis_connection import redis_client as _redis_client

    _redis_client._client = None
    _redis_client._connection_pool = None


def _principal(**overrides):
    from src.models.admin_auth import AdminPrincipal

    defaults = dict(
        email="regional@chokmoki.com",
        role="regional_admin",
        session_id="test-session",
        jti="test-jti",
        scopes=frozenset({"orders:read", "orders:write"}),
        regions=frozenset({"AU"}),
        is_root=False,
    )
    defaults.update(overrides)
    return AdminPrincipal(**defaults)


def _override(api_module, principal):
    from src.plugins.admin_deps import resolve_admin_principal

    api_module.app.dependency_overrides[resolve_admin_principal] = lambda: principal


def _clear_override(api_module):
    from src.plugins.admin_deps import resolve_admin_principal

    api_module.app.dependency_overrides.pop(resolve_admin_principal, None)


def _order_payload(country: str) -> dict:
    return {
        "user_email": "customer@example.com",
        "country": country,
        "shipping_address": {
            "full_name": "Test Customer",
            "phone": "+61400000000",
            "address_line1": "1 Test St",
            "city": "Sydney",
            "state": "NSW",
            "postal_code": "2000",
            "country": "Australia",
        },
        "items": [{"product_id": "prod1", "quantity": 1}],
        "payment_method": "cod",
    }


class _FakeOrder:
    def model_dump(self, by_alias=True):
        return {"order_id": "new-order-1"}


class TestAdminCreateOrderRegionScope:
    def test_regional_admin_can_create_order_in_own_region(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_orders.OrderService.create_from_admin",
                new_callable=AsyncMock,
                return_value=_FakeOrder(),
            ) as mock_create:
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.post("/api/admin/orders", json=_order_payload("AU"))
        finally:
            _clear_override(api_module)
        assert response.status_code == 200
        mock_create.assert_called_once()

    def test_regional_admin_cannot_create_order_outside_own_region(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_orders.OrderService.create_from_admin",
                new_callable=AsyncMock,
            ) as mock_create:
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.post("/api/admin/orders", json=_order_payload("IN"))
                mock_create.assert_not_called()
        finally:
            _clear_override(api_module)
        assert response.status_code == 400
        assert "AU" in response.json()["detail"]

    def test_multi_region_admin_can_create_order_in_either_assigned_region(self, api_module):
        principal = _principal(regions=frozenset({"AU", "IN"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_orders.OrderService.create_from_admin",
                new_callable=AsyncMock,
                return_value=_FakeOrder(),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response_in = client.post("/api/admin/orders", json=_order_payload("IN"))
                response_au = client.post("/api/admin/orders", json=_order_payload("AU"))
        finally:
            _clear_override(api_module)
        assert response_in.status_code == 200
        assert response_au.status_code == 200

    def test_root_can_create_order_for_any_region(self, api_module):
        principal = _principal(
            role="super_admin", scopes=frozenset({"*"}), regions=frozenset(), is_root=True
        )
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_orders.OrderService.create_from_admin",
                new_callable=AsyncMock,
                return_value=_FakeOrder(),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.post("/api/admin/orders", json=_order_payload("NZ"))
        finally:
            _clear_override(api_module)
        assert response.status_code == 200

    def test_admin_with_no_regions_assigned_is_unrestricted(self, api_module):
        principal = _principal(regions=frozenset())
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_orders.OrderService.create_from_admin",
                new_callable=AsyncMock,
                return_value=_FakeOrder(),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.post("/api/admin/orders", json=_order_payload("NZ"))
        finally:
            _clear_override(api_module)
        assert response.status_code == 200
