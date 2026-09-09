"""Route-level tests for the product price-only edit path
(api/routes/admin_catalog.py's require_any_scope + _assert_price_only_edit):
an admin holding only products:price_write (no full products:write) may
PATCH a product's `prices` array, and only rows whose country is one of
their assigned regions — every other field, and every other region's price
row, is rejected with 403 regardless of what the client sends."""
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
    # These tests exercise product price-scope logic through the real app —
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


def _principal(**overrides):
    from src.models.admin_auth import AdminPrincipal

    defaults = dict(
        email="admin@test.com",
        role="regional_admin",
        session_id="test-session",
        jti="test-jti",
        scopes=frozenset({"products:price_write"}),
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


class _Price:
    def __init__(self, country, sym, currency, mrp, sellingPrice):
        self.country = country
        self.sym = sym
        self.currency = currency
        self.mrp = mrp
        self.sellingPrice = sellingPrice


class _Stock:
    def __init__(self, country, qty, status):
        self.country = country
        self.qty = qty
        self.status = status


class _FakeProduct:
    def __init__(self, slug="ring-1", prices=None, stock=None):
        self.slug = slug
        self.prices = prices or [
            _Price("IN", "₹", "INR", 1000, 900),
            _Price("AU", "$", "AUD", 100, 90),
            _Price("default", "$", "USD", 50, 45),
        ]
        self.stock = stock or [
            _Stock("IN", 10, "in_stock"),
            _Stock("AU", 5, "in_stock"),
            _Stock("default", 8, "in_stock"),
        ]

    def model_dump(self, by_alias=True):
        return {
            "slug": self.slug,
            "prices": [vars(p) for p in self.prices],
            "stock": [vars(s) for s in self.stock],
        }


def _prices_payload(au_selling=95):
    return [
        {"country": "IN", "sym": "₹", "currency": "INR", "mrp": 1000, "sellingPrice": 900},
        {"country": "AU", "sym": "$", "currency": "AUD", "mrp": 100, "sellingPrice": au_selling},
        {"country": "default", "sym": "$", "currency": "USD", "mrp": 50, "sellingPrice": 45},
    ]


def _stock_payload(au_status="out_of_stock"):
    return [
        {"country": "IN", "qty": 10, "status": "in_stock"},
        {"country": "AU", "qty": 5, "status": au_status},
        {"country": "default", "qty": 8, "status": "in_stock"},
    ]


class TestProductPriceOnlyScope:
    def test_price_only_admin_can_update_own_region_price(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_catalog.ProductService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ), patch(
                "api.routes.admin_catalog.ProductService.update",
                new_callable=AsyncMock,
                return_value=_FakeProduct(prices=[
                    _Price("IN", "₹", "INR", 1000, 900),
                    _Price("AU", "$", "AUD", 100, 95),
                    _Price("default", "$", "USD", 50, 45),
                ]),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put(
                    "/api/admin/products/prod1",
                    json={"prices": _prices_payload(au_selling=95)},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200

    def test_price_only_admin_cannot_change_other_region_price(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_catalog.ProductService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ), patch(
                "api.routes.admin_catalog.ProductService.update",
                new_callable=AsyncMock,
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                payload = _prices_payload()
                payload[0]["sellingPrice"] = 850  # IN row changed
                response = client.put("/api/admin/products/prod1", json={"prices": payload})
        finally:
            _clear_override(api_module)
        assert response.status_code == 403
        assert "IN" in response.json()["detail"]

    def test_price_only_admin_cannot_change_default_bucket_price(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_catalog.ProductService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ), patch(
                "api.routes.admin_catalog.ProductService.update",
                new_callable=AsyncMock,
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                payload = _prices_payload()
                payload[2]["sellingPrice"] = 40  # default row changed
                response = client.put("/api/admin/products/prod1", json={"prices": payload})
        finally:
            _clear_override(api_module)
        assert response.status_code == 403
        assert "default" in response.json()["detail"]

    def test_price_only_admin_cannot_submit_name_or_category(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_catalog.ProductService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ), patch(
                "api.routes.admin_catalog.ProductService.update",
                new_callable=AsyncMock,
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put(
                    "/api/admin/products/prod1",
                    json={"prices": _prices_payload(), "name": "New name"},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 403
        assert "name" in response.json()["detail"]

    def test_price_only_admin_cannot_submit_price_inr_directly(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_catalog.ProductService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ), patch(
                "api.routes.admin_catalog.ProductService.update",
                new_callable=AsyncMock,
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put("/api/admin/products/prod1", json={"price_inr": 999})
        finally:
            _clear_override(api_module)
        assert response.status_code == 403

    def test_price_only_admin_cannot_create_product(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            client = TestClient(api_module.app, raise_server_exceptions=True)
            response = client.post("/api/admin/products", json={"name": "x"})
        finally:
            _clear_override(api_module)
        assert response.status_code == 403

    def test_price_only_admin_cannot_delete_product(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            client = TestClient(api_module.app, raise_server_exceptions=True)
            response = client.delete("/api/admin/products/prod1")
        finally:
            _clear_override(api_module)
        assert response.status_code == 403

    def test_full_write_admin_unaffected_by_price_only_check(self, api_module):
        principal = _principal(
            role="admin", scopes=frozenset({"products:write"}), regions=frozenset()
        )
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_catalog.ProductService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ), patch(
                "api.routes.admin_catalog.ProductService.update",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put(
                    "/api/admin/products/prod1",
                    json={"name": "New name", "prices": _prices_payload(au_selling=999)},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200

    def test_price_only_admin_can_update_own_region_stock(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_catalog.ProductService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ), patch(
                "api.routes.admin_catalog.ProductService.update",
                new_callable=AsyncMock,
                return_value=_FakeProduct(stock=[
                    _Stock("IN", 10, "in_stock"),
                    _Stock("AU", 5, "out_of_stock"),
                    _Stock("default", 8, "in_stock"),
                ]),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put(
                    "/api/admin/products/prod1",
                    json={"stock": _stock_payload(au_status="out_of_stock")},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200

    def test_price_only_admin_cannot_change_other_region_stock(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_catalog.ProductService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ), patch(
                "api.routes.admin_catalog.ProductService.update",
                new_callable=AsyncMock,
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                payload = _stock_payload()
                payload[0]["status"] = "out_of_stock"  # IN row changed
                response = client.put("/api/admin/products/prod1", json={"stock": payload})
        finally:
            _clear_override(api_module)
        assert response.status_code == 403
        assert "IN" in response.json()["detail"]

    def test_price_only_admin_can_submit_prices_and_stock_together(self, api_module):
        principal = _principal(regions=frozenset({"AU"}))
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_catalog.ProductService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ), patch(
                "api.routes.admin_catalog.ProductService.update",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                response = client.put(
                    "/api/admin/products/prod1",
                    json={
                        "prices": _prices_payload(au_selling=99),
                        "stock": _stock_payload(au_status="out_of_stock"),
                    },
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200

    def test_root_price_edit_any_field_any_region(self, api_module):
        principal = _principal(
            role="super_admin", scopes=frozenset({"*"}), regions=frozenset(), is_root=True
        )
        _override(api_module, principal)
        try:
            with patch(
                "api.routes.admin_catalog.ProductService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ), patch(
                "api.routes.admin_catalog.ProductService.update",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ):
                client = TestClient(api_module.app, raise_server_exceptions=True)
                payload = _prices_payload()
                payload[0]["sellingPrice"] = 850
                response = client.put(
                    "/api/admin/products/prod1",
                    json={"name": "New name", "prices": payload},
                )
        finally:
            _clear_override(api_module)
        assert response.status_code == 200
