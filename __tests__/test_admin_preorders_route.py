"""Route-level RBAC/region-scoping tests for the admin pre-order endpoints
— mirrors test_regional_admin_orders_write.py's style (mocked service,
dependency-overridden principal) for GET list / PATCH status / CSV
export."""
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
        scopes=frozenset({"preorders:read", "preorders:write"}),
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


def _doc(_id="507f1f77bcf86cd799439011", region="AU", status="new"):
    from datetime import datetime

    return {
        "_id": _id,
        "product_id": "prod1",
        "product_name": "Test Ring",
        "product_slug": "test-ring",
        "region": region,
        "name": "Jane Doe",
        "email": "jane@example.com",
        "phone": "+61400000000",
        "quantity": 2,
        "size": "M",
        "notify_via": ["email"],
        "message": "",
        "status": status,
        "ip": "103.59.73.243",
        "created_at": datetime(2026, 1, 1),
        "updated_at": datetime(2026, 1, 1),
    }


class TestRouteRegistered:
    def test_routes_registered(self, api_module):
        paths = {route.path for route in api_module.app.routes}
        assert "/api/admin/preorders" in paths
        assert "/api/admin/preorders/{preorder_id}/status" in paths
        assert "/api/admin/preorders/export.csv" in paths
        assert "/api/preorders" in paths


class TestRequiresAdmin:
    def test_unauthenticated_list_is_401(self, api_module):
        client = TestClient(api_module.app, raise_server_exceptions=True)
        response = client.get("/api/admin/preorders")
        assert response.status_code == 401


class TestScopeEnforcement:
    def test_missing_preorders_scope_is_403(self, api_module):
        principal = _principal(scopes=frozenset({"orders:read", "orders:write"}))
        _override(api_module, principal)
        try:
            client = TestClient(api_module.app, raise_server_exceptions=True)
            response = client.get("/api/admin/preorders")
        finally:
            _clear_override(api_module)
        assert response.status_code == 403


class TestAdminListRegionScoping:
    def test_au_scoped_admin_only_ever_sees_au_rows_even_when_requesting_in(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with (
                patch("api.routes.admin_preorders.cache", None),
                patch(
                    "api.routes.admin_preorders.PreorderService.list", new_callable=AsyncMock
                ) as mock_list,
                patch(
                    "api.routes.admin_preorders.PreorderService.count", new_callable=AsyncMock
                ) as mock_count,
            ):
                mock_list.return_value = [_doc()]
                mock_count.return_value = 1
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.get("/api/admin/preorders", params={"region": "IN"})
        finally:
            _clear_override(api_module)
        assert response.status_code == 200
        # narrowing failed (IN not in admin's AU set) -> falls back to full
        # allowed set, never IN itself, and never None (unrestricted).
        assert mock_list.call_args.kwargs["regions"] == ["AU"]
        assert mock_list.call_args.kwargs["region"] is None

    def test_list_response_includes_submitter_ip(self, api_module):
        """Regression: an admin needs the submitter's IP to diagnose a
        region mismatch (e.g. a customer who selected AU on the storefront
        but whose stored `region` came back "IN" because the client never
        sent selectedCountry at all, so resolution fell through to pure
        GeoIP) — public_row() must not strip `ip` from the admin response,
        only from what the public POST endpoint itself returns."""
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with (
                patch("api.routes.admin_preorders.cache", None),
                patch(
                    "api.routes.admin_preorders.PreorderService.list", new_callable=AsyncMock
                ) as mock_list,
                patch(
                    "api.routes.admin_preorders.PreorderService.count", new_callable=AsyncMock
                ) as mock_count,
            ):
                mock_list.return_value = [_doc()]
                mock_count.return_value = 1
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.get("/api/admin/preorders")
        finally:
            _clear_override(api_module)
        assert response.status_code == 200
        assert response.json()["data"][0]["ip"] == "103.59.73.243"

    def test_root_admin_sees_everything_unrestricted(self, api_module):
        principal = _principal(
            role="super_admin", scopes=frozenset({"*"}), regions=frozenset(), is_root=True
        )
        _override(api_module, principal)
        try:
            with (
                patch("api.routes.admin_preorders.cache", None),
                patch(
                    "api.routes.admin_preorders.PreorderService.list", new_callable=AsyncMock
                ) as mock_list,
                patch(
                    "api.routes.admin_preorders.PreorderService.count", new_callable=AsyncMock
                ) as mock_count,
            ):
                mock_list.return_value = []
                mock_count.return_value = 0
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.get("/api/admin/preorders")
        finally:
            _clear_override(api_module)
        assert response.status_code == 200
        assert mock_list.call_args.kwargs["regions"] is None

    def test_pagination_count_is_forwarded_accurately(self, api_module):
        principal = _principal(regions=frozenset())
        _override(api_module, principal)
        try:
            with (
                patch("api.routes.admin_preorders.cache", None),
                patch(
                    "api.routes.admin_preorders.PreorderService.list", new_callable=AsyncMock
                ) as mock_list,
                patch(
                    "api.routes.admin_preorders.PreorderService.count", new_callable=AsyncMock
                ) as mock_count,
            ):
                mock_list.return_value = [_doc()]
                mock_count.return_value = 42
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.get("/api/admin/preorders", params={"skip": 10, "limit": 5})
        finally:
            _clear_override(api_module)
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 42
        assert len(body["data"]) == 1
        assert mock_list.call_args.kwargs["skip"] == 10
        assert mock_list.call_args.kwargs["limit"] == 5


class TestAdminStatusUpdate:
    def test_404_not_403_when_preorder_outside_admin_region_scope(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with (
                patch(
                    "api.routes.admin_preorders.PreorderService.get_by_id",
                    new_callable=AsyncMock,
                    return_value=_doc(region="IN"),
                ),
                patch(
                    "api.routes.admin_preorders.PreorderService.update_status",
                    new_callable=AsyncMock,
                ) as mock_update,
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.patch(
                    "/api/admin/preorders/507f1f77bcf86cd799439011/status",
                    json={"status": "notified"},
                )
                mock_update.assert_not_called()
        finally:
            _clear_override(api_module)
        assert response.status_code == 404

    def test_in_region_status_update_succeeds(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with (
                patch(
                    "api.routes.admin_preorders.PreorderService.get_by_id",
                    new_callable=AsyncMock,
                    return_value=_doc(region="AU"),
                ),
                patch(
                    "api.routes.admin_preorders.PreorderService.update_status",
                    new_callable=AsyncMock,
                    return_value=_doc(region="AU", status="notified"),
                ),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.patch(
                    "/api/admin/preorders/507f1f77bcf86cd799439011/status",
                    json={"status": "notified"},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200
        assert response.json()["status"] == "notified"

    def test_invalid_status_value_is_422(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_preorders.PreorderService.get_by_id",
                new_callable=AsyncMock,
                return_value=_doc(region="AU"),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.patch(
                    "/api/admin/preorders/507f1f77bcf86cd799439011/status",
                    json={"status": "bogus"},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 422


class TestCsvExportRegionScoping:
    def test_export_forwards_admins_forced_regions_not_client_region(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_preorders.PreorderService.list", new_callable=AsyncMock
            ) as mock_list:
                mock_list.return_value = [_doc()]
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.get(
                    "/api/admin/preorders/export.csv", params={"region": "IN"}
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert mock_list.call_args.kwargs["regions"] == ["AU"]
        assert mock_list.call_args.kwargs["region"] is None
        body = response.text
        assert "name,email,phone,product_name,quantity,size,notify_via,message,status,region,ip,created_at" in body
        assert "Jane Doe" in body
