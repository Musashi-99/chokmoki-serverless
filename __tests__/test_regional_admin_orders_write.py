"""Regression test for "venom can't write order — region is assigned, so
they can do the admin work, else what will be the meaning of admin user
for a region?": regional admins previously only got orders:read by
design ("view order" only, per the original spec). The site owner
clarified they actually need to run their region's order operations
(update status, pack, etc.), not just view them. REGIONAL_ADMIN's default
scope set now includes orders:write — safe to grant broadly because every
order-mutation route already enforces region scope independently via
admin_orders.py's _enforce_order_region (404s any order outside
principal.regions regardless of scope), proven here at the route level,
not just via the already-covered _enforce_order_region unit test in
test_region.py."""
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


class _FakeOrder:
    def __init__(self, order_id, country):
        self.order_id = order_id
        self.region_audit = types.SimpleNamespace(pricing_country_used=country)
        self.fulfillment_status = "unpacked"

    def model_dump(self, by_alias=True):
        return {"order_id": self.order_id}


class TestRegionalAdminOrdersWrite:
    def test_regional_admin_with_orders_write_scope_is_no_longer_403d(self, api_module):
        """Confirms the specific bug reported: a regional admin previously
        got 403 'Missing scope: orders:write' just for holding
        orders:read. Scope check alone must now pass — region enforcement
        (tested below) is what actually gates access."""
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_orders.OrderService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeOrder("ORD1", "AU"),
            ), patch(
                "api.routes.admin_orders.OrderService.update_status",
                new_callable=AsyncMock,
                return_value=_FakeOrder("ORD1", "AU"),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put(
                    "/api/admin/orders/ORD1/status",
                    json={"status": {"type": "accepted"}},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code != 403

    def test_regional_admin_can_update_status_for_in_region_order(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_orders.OrderService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeOrder("ORD1", "AU"),
            ), patch(
                "api.routes.admin_orders.OrderService.update_status",
                new_callable=AsyncMock,
                return_value=_FakeOrder("ORD1", "AU"),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put(
                    "/api/admin/orders/ORD1/status",
                    json={"status": {"type": "accepted"}},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200

    def test_regional_admin_cannot_update_status_for_out_of_region_order(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_orders.OrderService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeOrder("ORD2", "IN"),
            ), patch(
                "api.routes.admin_orders.OrderService.update_status",
                new_callable=AsyncMock,
            ) as mock_update:
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put(
                    "/api/admin/orders/ORD2/status",
                    json={"status": {"type": "accepted"}},
                )
                mock_update.assert_not_called()
        finally:
            _clear_override(api_module)
        assert response.status_code == 404

    def test_regional_admin_can_mark_in_region_order_packed(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_orders.OrderService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeOrder("ORD1", "AU"),
            ), patch(
                "api.routes.admin_orders.db.get_database",
                new_callable=AsyncMock,
            ) as mock_get_db, patch(
                "api.routes.admin_orders.order_ledger.append_event",
                new_callable=AsyncMock,
            ):
                mock_collection = AsyncMock()
                mock_db = {"orders": mock_collection}
                mock_get_db.return_value = mock_db
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.post("/api/admin/orders/ORD1/pack")
        finally:
            _clear_override(api_module)
        assert response.status_code == 200

    def test_regional_admin_cannot_mark_out_of_region_order_packed(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_orders.OrderService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeOrder("ORD2", "IN"),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.post("/api/admin/orders/ORD2/pack")
        finally:
            _clear_override(api_module)
        assert response.status_code == 404

    def test_admin_with_only_orders_read_still_403s_on_write(self, api_module):
        principal = _principal(scopes=frozenset({"orders:read"}), regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            client = TestClient(api_module.app, raise_server_exceptions=True)
            response = client.put(
                "/api/admin/orders/ORD1/status",
                json={"status": {"type": "accepted"}},
            )
        finally:
            _clear_override(api_module)
        assert response.status_code == 403

    def test_root_can_update_status_for_any_region_order(self, api_module):
        principal = _principal(
            role="super_admin", scopes=frozenset({"*"}), regions=frozenset(), is_root=True
        )
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_orders.OrderService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeOrder("ORD2", "IN"),
            ), patch(
                "api.routes.admin_orders.OrderService.update_status",
                new_callable=AsyncMock,
                return_value=_FakeOrder("ORD2", "IN"),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put(
                    "/api/admin/orders/ORD2/status",
                    json={"status": {"type": "accepted"}},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200
