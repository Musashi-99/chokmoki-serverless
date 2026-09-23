"""Route-level tests for POST /api/preorders — validation rejections and
the happy path, with ProductService/PreorderService mocked (no DB
needed)."""
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


def _valid_payload(**overrides):
    payload = {
        "productId": "prod-1",
        "name": "Jane Doe",
        "email": "jane@example.com",
        "phone": "+61400000000",
        "quantity": 2,
        "size": "M",
        "notifyVia": ["email"],
        "message": "notify me please",
        "selectedCountry": "AU",
    }
    payload.update(overrides)
    return payload


class _FakeProduct:
    name = "Test Ring"
    slug = "test-ring"


class TestPublicPreorderCreate:
    def test_happy_path_returns_id_and_status(self, api_module):
        with (
            patch(
                "api.routes.preorders.ProductService.get_by_id",
                new_callable=AsyncMock,
                return_value=_FakeProduct(),
            ),
            patch(
                "api.routes.preorders.PreorderService.resolve_region",
                new_callable=AsyncMock,
                return_value="AU",
            ),
            patch(
                "api.routes.preorders.PreorderService.record",
                new_callable=AsyncMock,
                return_value={"_id": "abc123", "status": "new"},
            ) as mock_record,
        ):
            client = TestClient(api_module.app, raise_server_exceptions=True)
            response = client.post("/api/preorders", json=_valid_payload())

        assert response.status_code == 201
        body = response.json()
        assert body == {"id": "abc123", "status": "new"}
        assert mock_record.call_args.kwargs["region"] == "AU"

    def test_empty_notify_via_is_rejected(self, api_module):
        client = TestClient(api_module.app, raise_server_exceptions=True)
        response = client.post("/api/preorders", json=_valid_payload(notifyVia=[]))
        assert response.status_code == 422

    def test_quantity_out_of_range_is_rejected(self, api_module):
        client = TestClient(api_module.app, raise_server_exceptions=True)
        response = client.post("/api/preorders", json=_valid_payload(quantity=11))
        assert response.status_code == 422

        response2 = client.post("/api/preorders", json=_valid_payload(quantity=0))
        assert response2.status_code == 422

    def test_sms_without_phone_is_rejected(self, api_module):
        client = TestClient(api_module.app, raise_server_exceptions=True)
        response = client.post(
            "/api/preorders",
            json=_valid_payload(notifyVia=["sms"], phone=None),
        )
        assert response.status_code == 422

    def test_unknown_product_is_404(self, api_module):
        with patch(
            "api.routes.preorders.ProductService.get_by_id",
            new_callable=AsyncMock,
            return_value=None,
        ):
            client = TestClient(api_module.app, raise_server_exceptions=True)
            response = client.post("/api/preorders", json=_valid_payload())
        assert response.status_code == 404
