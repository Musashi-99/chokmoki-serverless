"""Route wiring tests for GET /api/admin/orders/export — the raw data source
the unified export ZIP embeds as orders/orders.json + orders/order_logs.json.
Restore now goes through POST /api/admin/import only (see
test_admin_import_route.py); there is no standalone orders import endpoint."""

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


class TestAdminOrdersExportRoute:
    def test_route_registered(self, api_module):
        paths = {route.path for route in api_module.app.routes}
        assert "/api/admin/orders/export" in paths
        assert "/api/admin/orders/import" not in paths

    def test_export_requires_admin(self, api_module):
        client = TestClient(api_module.app, raise_server_exceptions=True)
        response = client.get("/api/admin/orders/export")
        assert response.status_code == 401

    def test_export_returns_downloadable_json(self, api_module):
        from src.models.admin_auth import AdminPrincipal
        from src.plugins.admin_deps import resolve_admin_principal

        fake_principal = AdminPrincipal(
            email="admin@test.com",
            role="super_admin",
            session_id="test-session",
            jti="test-jti",
            scopes=frozenset({"*"}),
            regions=frozenset(),
            is_root=True,
        )
        api_module.app.dependency_overrides[resolve_admin_principal] = lambda: fake_principal
        try:
            with (
                patch("api.routes.admin_orders_backup.export_orders_backup", new_callable=AsyncMock) as mock_export,
                patch("api.routes.admin_orders_backup.db.get_database", new_callable=AsyncMock) as mock_get_database,
            ):
                mock_get_database.return_value = object()
                mock_export.return_value = {"exported_at": "now", "generator": "test", "orders": [], "order_logs": []}
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.get("/api/admin/orders/export")
        finally:
            api_module.app.dependency_overrides.pop(resolve_admin_principal, None)

        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert response.json()["generator"] == "test"
