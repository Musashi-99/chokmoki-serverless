"""Route wiring tests for POST /api/cart-activity — the public, unauthenticated
guest-lead capture endpoint. Uses a REAL Mongo connection for the happy-path
persistence check (this project's established pattern), and mocks the
service for the validation-only cases."""
from __future__ import annotations

import importlib
import os
import sys
import types
import uuid
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


class TestRouteRegistered:
    def test_route_registered(self, api_module):
        paths = {route.path for route in api_module.app.routes}
        assert "/api/cart-activity" in paths


class TestNoAuthRequired:
    def test_empty_body_is_accepted_not_401(self, api_module):
        client = TestClient(api_module.app, raise_server_exceptions=True)
        response = client.post("/api/cart-activity", json={})
        assert response.status_code == 200
        assert response.json() == {"recorded": False}


class TestValidation:
    def test_invalid_email_rejected_422(self, api_module):
        client = TestClient(api_module.app, raise_server_exceptions=True)
        response = client.post("/api/cart-activity", json={"email": "not-an-email"})
        assert response.status_code == 422

    def test_too_short_phone_rejected_422(self, api_module):
        client = TestClient(api_module.app, raise_server_exceptions=True)
        response = client.post("/api/cart-activity", json={"phone": "123"})
        assert response.status_code == 422

    def test_malformed_payload_rejected_422(self, api_module):
        client = TestClient(api_module.app, raise_server_exceptions=True)
        response = client.post("/api/cart-activity", json={"cartItems": "not-a-list"})
        assert response.status_code == 422


class TestServiceFailureNeverSurfacesAsAnError:
    def test_service_exception_still_returns_200_recorded_false(self, api_module):
        with patch(
            "api.routes.cart_activity.AbandonedCartService.record",
            new_callable=AsyncMock,
        ) as mock_record:
            mock_record.side_effect = RuntimeError("mongo is down")
            client = TestClient(api_module.app, raise_server_exceptions=True)
            response = client.post(
                "/api/cart-activity", json={"email": "shopper@example.com"}
            )
        assert response.status_code == 200
        assert response.json() == {"recorded": False}


class TestHappyPathPersistsToRealMongo:
    @pytest.fixture
    def api_module(self, monkeypatch):
        # A dedicated override of the module fixture: this class needs a
        # REAL, reachable Mongo (CI's service container listens on the
        # standard 27017; local dev may run it on a different host port
        # to avoid clashing with another project's container — override
        # via TEST_MONGODB_URI when that's the case).
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv(
            "MONGODB_URI", os.environ.get("TEST_MONGODB_URI", "mongodb://localhost:27017")
        )
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
        _stub_external_modules()
        if "api.index" in sys.modules:
            del sys.modules["api.index"]
        return importlib.import_module("api.index")

    def test_valid_email_and_cart_is_recorded(self, api_module, monkeypatch):
        from src.config import settings
        from src.database.connection import db

        # src.config's `settings` singleton only reads MONGODB_URI at first
        # import in the whole pytest session — by the time this class's
        # fixture runs, an earlier test file/class may have already
        # triggered that with the hardcoded 27017 default, making our own
        # monkeypatch.setenv above a no-op. Patch the already-constructed
        # object directly instead, so this test is robust to run order.
        mongo_uri = os.environ.get("TEST_MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setattr(settings, "mongodb_uri", mongo_uri)
        db._client = None
        email = f"route-test-{uuid.uuid4().hex[:10]}@example.com"
        client = TestClient(api_module.app, raise_server_exceptions=True)
        response = client.post(
            "/api/cart-activity",
            json={
                "email": email,
                "cartItems": [
                    {"productId": "p1", "productName": "Ring", "quantity": 1,
                     "price": 999.0, "total": 999.0, "currency": "INR", "sym": "₹"}
                ],
                "selectedCountry": "IN",
                "source": "checkout",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"recorded": True}

        # Sync pymongo (not the async motor client TestClient's own event
        # loop already holds) so cleanup doesn't need a second event loop.
        import pymongo
        from src.config import settings

        sync_client = pymongo.MongoClient(os.environ["MONGODB_URI"])
        try:
            sync_client[settings.mongodb_db_name]["abandoned_carts"].delete_many(
                {"email_normalized": email}
            )
        finally:
            sync_client.close()
        db._client = None
