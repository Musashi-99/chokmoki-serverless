"""Route wiring tests for POST /api/admin/import."""

from __future__ import annotations

import importlib
import io
import os
import sys
import types
import zipfile
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


def _minimal_bundle_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(
            "sections/faq/content.json",
            '{"exported_at": "2026-07-09T00:00:00Z", "generator": "test", "faq": []}',
        )
        zf.writestr(
            "manifest.csv",
            "Path in ZIP,Entity,Field,Original Source URL,Status,Bytes\n",
        )
    return buf.getvalue()


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


class TestAdminImportRoute:
    def test_route_registered(self, api_module):
        paths = {route.path for route in api_module.app.routes}
        assert "/api/admin/import" in paths

    def test_requires_admin(self, api_module):
        client = TestClient(api_module.app, raise_server_exceptions=True)
        files = {"bundle": ("backup.zip", _minimal_bundle_zip(), "application/zip")}
        response = client.post("/api/admin/import", files=files)
        assert response.status_code == 401

    def test_accepts_zip_and_returns_summary(self, api_module):
        from src.models.admin_auth import AdminPrincipal
        from src.plugins.admin_deps import resolve_admin_principal

        fake_principal = AdminPrincipal(
            email="admin@test.com",
            role="super_admin",
            session_id="test-session",
            jti="test-jti",
            scopes=frozenset({"*"}),
            region=None,
            is_root=True,
        )
        api_module.app.dependency_overrides[resolve_admin_principal] = lambda: fake_principal
        try:
            with (
                patch("api.routes.admin_import.restore_bundle", new_callable=AsyncMock) as mock_restore,
                patch("api.routes.admin_import.db.get_database", new_callable=AsyncMock) as mock_get_database,
                patch("api.routes.admin_import.R2Service"),
            ):
                from src.services.import_service import ImportResult

                mock_get_database.return_value = object()
                mock_restore.return_value = ImportResult(
                    sections_restored=["faq"], sections_skipped=[], assets_restored=0, assets_failed=0
                )
                client = TestClient(api_module.app, raise_server_exceptions=True)
                files = {"bundle": ("backup.zip", _minimal_bundle_zip(), "application/zip")}
                response = client.post("/api/admin/import", files=files)
        finally:
            api_module.app.dependency_overrides.pop(resolve_admin_principal, None)

        assert response.status_code == 200
        body = response.json()
        assert body["sections_restored"] == ["faq"]
        assert body["assets_restored"] == 0

    def test_rejects_invalid_zip_with_400(self, api_module):
        from src.models.admin_auth import AdminPrincipal
        from src.plugins.admin_deps import resolve_admin_principal

        fake_principal = AdminPrincipal(
            email="admin@test.com",
            role="super_admin",
            session_id="test-session",
            jti="test-jti",
            scopes=frozenset({"*"}),
            region=None,
            is_root=True,
        )
        api_module.app.dependency_overrides[resolve_admin_principal] = lambda: fake_principal
        try:
            client = TestClient(api_module.app, raise_server_exceptions=True)
            files = {"bundle": ("backup.zip", b"not a zip", "application/zip")}
            response = client.post("/api/admin/import", files=files)
        finally:
            api_module.app.dependency_overrides.pop(resolve_admin_principal, None)

        assert response.status_code == 400

    def test_rejects_oversized_bundle_with_400(self, api_module, monkeypatch):
        from src.models.admin_auth import AdminPrincipal
        from src.plugins.admin_deps import resolve_admin_principal
        import api.routes.admin_import as admin_import_module

        monkeypatch.setattr(admin_import_module, "MAX_BUNDLE_BYTES", 10)
        fake_principal = AdminPrincipal(
            email="admin@test.com",
            role="super_admin",
            session_id="test-session",
            jti="test-jti",
            scopes=frozenset({"*"}),
            region=None,
            is_root=True,
        )
        api_module.app.dependency_overrides[resolve_admin_principal] = lambda: fake_principal
        try:
            client = TestClient(api_module.app, raise_server_exceptions=True)
            files = {"bundle": ("backup.zip", _minimal_bundle_zip(), "application/zip")}
            response = client.post("/api/admin/import", files=files)
        finally:
            api_module.app.dependency_overrides.pop(resolve_admin_principal, None)

        assert response.status_code == 400
        assert "exceeds maximum size" in response.json()["detail"]

    def test_dry_run_does_not_call_restore_bundle(self, api_module):
        from src.models.admin_auth import AdminPrincipal
        from src.plugins.admin_deps import resolve_admin_principal

        fake_principal = AdminPrincipal(
            email="admin@test.com",
            role="super_admin",
            session_id="test-session",
            jti="test-jti",
            scopes=frozenset({"*"}),
            region=None,
            is_root=True,
        )
        api_module.app.dependency_overrides[resolve_admin_principal] = lambda: fake_principal
        try:
            with (
                patch("api.routes.admin_import.restore_bundle", new_callable=AsyncMock) as mock_restore,
                patch("api.routes.admin_import.plan_restore", new_callable=AsyncMock) as mock_plan,
                patch("api.routes.admin_import.db.get_database", new_callable=AsyncMock) as mock_get_database,
            ):
                from src.services.import_service import RestorePlan

                mock_get_database.return_value = object()
                mock_plan.return_value = RestorePlan(sections_to_restore=["faq"], assets_to_upload=1)
                client = TestClient(api_module.app, raise_server_exceptions=True)
                files = {"bundle": ("backup.zip", _minimal_bundle_zip(), "application/zip")}
                response = client.post("/api/admin/import?dry_run=true", files=files)
        finally:
            api_module.app.dependency_overrides.pop(resolve_admin_principal, None)

        assert response.status_code == 200
        mock_restore.assert_not_called()
        mock_plan.assert_awaited_once()
        assert response.json()["sections_to_restore"] == ["faq"]
