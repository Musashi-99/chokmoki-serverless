"""Route-level tests for the coupon region-scoping fix
(api/routes/admin_coupons.py's _assert_coupon_countries_in_scope): a
regional admin may only create/update coupons whose `countries` are a
subset of their own assigned regions, and the four coupon routes now
require coupons:read/coupons:write (split off orders:write) instead."""
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
    # These tests exercise coupon-scope/region logic through the real app —
    # disable rate limiting so RateLimitMiddleware doesn't make a REAL Redis
    # call per request (the module-level `settings` singleton may already be
    # constructed from an earlier test file's import, so mutate the instance
    # directly rather than relying on the env var being re-read).
    from src.config import settings as _settings

    monkeypatch.setattr(_settings, "rate_limit_enabled", False)
    yield module

    # This test file is the first to establish a real Redis connection via
    # TestClient requests in some collection orders; left dangling, that
    # connection is bound to THIS test's (function-scoped) event loop and
    # breaks a later test file that does its own real-Redis check on a new
    # loop (the same "singleton bound to a closed event loop" class of bug
    # as scripts/smoke_test_region_scoping.py's _run_async and
    # test_user_null_identifier_regression.py's Mongo-singleton reset).
    from src.database.redis_connection import redis_client as _redis_client

    _redis_client._client = None
    _redis_client._connection_pool = None


def _principal(api_module, **overrides):
    from src.models.admin_auth import AdminPrincipal

    defaults = dict(
        email="admin@test.com",
        role="admin",
        session_id="test-session",
        jti="test-jti",
        scopes=frozenset({"coupons:read", "coupons:write"}),
        regions=frozenset(),
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


class _FakeCoupon:
    def __init__(self, code="SAVE10", countries=None):
        self.code = code
        self.type = "CART"
        self.amount = 10
        self.indicator = "PERCENT"
        self.product_ids = None
        self.countries = countries
        self.active = True

    def model_dump(self, by_alias=True):
        return {
            "code": self.code,
            "type": self.type,
            "amount": self.amount,
            "indicator": self.indicator,
            "product_ids": self.product_ids,
            "countries": self.countries,
            "active": self.active,
        }


class TestCouponRegionScope:
    def test_regional_admin_create_coupon_within_region_allowed(self, api_module):
        principal = _principal(
            api_module,
            role="regional_admin",
            scopes=frozenset({"coupons:read", "coupons:write"}),
            regions=frozenset({"AU"}),
        )
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_coupons.CouponService.create",
                new_callable=AsyncMock,
                return_value=_FakeCoupon(countries=["AU"]),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.post(
                    "/api/admin/coupons",
                    json={"code": "SAVEAU", "type": "CART", "amount": 10, "indicator": "PERCENT", "countries": ["AU"]},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200

    def test_regional_admin_create_coupon_with_no_countries_rejected(self, api_module):
        """Empty/omitted `countries` means "valid everywhere" (see
        src/models/coupon.py) — MORE access than any single region, so a
        regional admin leaving every checkbox unchecked must be rejected,
        not treated as having nothing to validate."""
        principal = _principal(
            api_module,
            role="regional_admin",
            scopes=frozenset({"coupons:read", "coupons:write"}),
            regions=frozenset({"AU"}),
        )
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_coupons.CouponService.create",
                new_callable=AsyncMock,
                return_value=_FakeCoupon(countries=None),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.post(
                    "/api/admin/coupons",
                    json={"code": "SAVEALL", "type": "CART", "amount": 10, "indicator": "PERCENT"},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 400

    def test_regional_admin_create_coupon_outside_region_rejected(self, api_module):
        principal = _principal(
            api_module,
            role="regional_admin",
            scopes=frozenset({"coupons:read", "coupons:write"}),
            regions=frozenset({"AU"}),
        )
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_coupons.CouponService.create",
                new_callable=AsyncMock,
                return_value=_FakeCoupon(countries=["AU", "NZ"]),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.post(
                    "/api/admin/coupons",
                    json={
                        "code": "SAVEAUNZ",
                        "type": "CART",
                        "amount": 10,
                        "indicator": "PERCENT",
                        "countries": ["AU", "NZ"],
                    },
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 400
        assert "NZ" in response.json()["detail"]

    def test_regional_admin_update_coupon_widening_countries_rejected(self, api_module):
        principal = _principal(
            api_module,
            role="regional_admin",
            scopes=frozenset({"coupons:read", "coupons:write"}),
            regions=frozenset({"AU"}),
        )
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_coupons.CouponService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeCoupon(countries=["AU"]),
            ), patch(
                "api.routes.admin_coupons.CouponService.update",
                new_callable=AsyncMock,
                return_value=_FakeCoupon(countries=["AU", "IN"]),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put(
                    "/api/admin/coupons/abc123",
                    json={"countries": ["AU", "IN"]},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 400
        assert "IN" in response.json()["detail"]

    def test_regional_admin_update_coupon_narrowing_within_region_allowed(self, api_module):
        principal = _principal(
            api_module,
            role="regional_admin",
            scopes=frozenset({"coupons:read", "coupons:write"}),
            regions=frozenset({"AU"}),
        )
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_coupons.CouponService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeCoupon(countries=["AU"]),
            ), patch(
                "api.routes.admin_coupons.CouponService.update",
                new_callable=AsyncMock,
                return_value=_FakeCoupon(countries=["AU"]),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put(
                    "/api/admin/coupons/abc123",
                    json={"countries": ["AU"]},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200

    def test_root_create_coupon_any_countries_allowed(self, api_module):
        principal = _principal(
            api_module,
            role="super_admin",
            scopes=frozenset({"*"}),
            regions=frozenset(),
            is_root=True,
        )
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_coupons.CouponService.create",
                new_callable=AsyncMock,
                return_value=_FakeCoupon(countries=["IN", "AU", "NZ", "default"]),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.post(
                    "/api/admin/coupons",
                    json={
                        "code": "SAVEALL",
                        "type": "CART",
                        "amount": 10,
                        "indicator": "PERCENT",
                        "countries": ["IN", "AU", "NZ", "default"],
                    },
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200

    def test_admin_without_coupons_write_scope_403(self, api_module):
        principal = _principal(api_module, scopes=frozenset({"orders:write"}))
        _override(api_module, principal)
        try:
            client = TestClient(api_module.app, raise_server_exceptions=True)
            response = client.post(
                "/api/admin/coupons",
                json={"code": "SAVE10", "type": "CART", "amount": 10, "indicator": "PERCENT"},
            )
        finally:
            _clear_override(api_module)
        assert response.status_code == 403

    def test_list_coupons_requires_coupons_read_not_orders_write(self, api_module):
        principal = _principal(api_module, scopes=frozenset({"orders:write"}))
        _override(api_module, principal)
        try:
            client = TestClient(api_module.app, raise_server_exceptions=True)
            response = client.get("/api/admin/coupons")
        finally:
            _clear_override(api_module)
        assert response.status_code == 403
