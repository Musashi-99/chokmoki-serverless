"""F-03 trusted proxy handling, rate-limit hardening, and login lockout tests."""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Settings
from src.security.client_ip import (
    extract_client_ip,
    get_client_ip,
    should_fail_closed_for_path,
)
from src.security.exceptions import AccountLockedError
from src.security.login_lockout import LoginLockoutService
from src.security.password import hash_password
from src.plugins.rate_limit import RateLimitMiddleware

STRONG_ADMIN_HASH = hash_password("production-admin-pass-99!")


def _make_request(
    *,
    path: str = "/",
    method: str = "POST",
    headers: dict | None = None,
    client_host: str = "127.0.0.1",
    query_string: bytes = b"",
) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string,
        "headers": [
            (key.lower().encode(), value.encode())
            for key, value in (headers or {}).items()
        ],
        "client": (client_host, 12345),
        "server": ("testserver", 80),
        "scheme": "http",
        "http_version": "1.1",
    }
    return Request(scope)


class TestClientIpExtraction:
    def test_ignores_spoofed_xff_on_vercel(self, monkeypatch):
        monkeypatch.setenv("VERCEL", "1")
        request = _make_request(
            headers={
                "X-Forwarded-For": "1.2.3.99",
                "x-vercel-forwarded-for": "203.0.113.10",
            }
        )
        assert extract_client_ip(request) == "203.0.113.10"

    def test_ignores_spoofed_xff_without_trust(self, monkeypatch):
        monkeypatch.delenv("VERCEL", raising=False)
        request = _make_request(headers={"X-Forwarded-For": "1.2.3.99"})
        assert extract_client_ip(request) == "127.0.0.1"

    def test_uses_real_ip_when_trusted_proxy_enabled(self, monkeypatch):
        monkeypatch.delenv("VERCEL", raising=False)
        request = _make_request(headers={"X-Real-IP": "198.51.100.20"})
        assert (
            extract_client_ip(request, trusted_proxy_enabled=True)
            == "198.51.100.20"
        )

    def test_custom_header_override(self, monkeypatch):
        monkeypatch.delenv("VERCEL", raising=False)
        request = _make_request(headers={"CF-Connecting-IP": "192.0.2.44"})
        assert (
            extract_client_ip(request, custom_header="CF-Connecting-IP")
            == "192.0.2.44"
        )

    def test_get_client_ip_wrapper(self, monkeypatch):
        monkeypatch.setenv("VERCEL", "1")
        request = _make_request(headers={"x-vercel-forwarded-for": "203.0.113.55"})
        assert get_client_ip(request) == "203.0.113.55"


class TestFailClosedPolicy:
    def test_auth_routes_fail_closed_by_default(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
        monkeypatch.setenv("RATE_LIMIT_AUTH_FAIL_CLOSED", "true")
        monkeypatch.setenv("RATE_LIMIT_FAIL_CLOSED", "false")

        assert should_fail_closed_for_path("/api/admin/login") is True
        assert should_fail_closed_for_path("/api/products") is False


class TestRateLimitMiddlewareFailClosed:
    @pytest.mark.asyncio
    async def test_auth_route_blocks_when_redis_unavailable(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("RATE_LIMIT_AUTH_FAIL_CLOSED", "true")

        middleware = RateLimitMiddleware(app=AsyncMock())
        request = _make_request(
            path="/api/admin/login",
            method="POST",
            headers={"content-type": "application/json"},
        )

        async def receive():
            return {
                "type": "http.request",
                "body": b'{"email":"a@b.com","password":"x"}',
            }

        request._receive = receive

        with patch(
            "src.plugins.rate_limit.redis_client.get_client",
            new_callable=AsyncMock,
            side_effect=RuntimeError("redis down"),
        ):
            response = await middleware.dispatch(request, AsyncMock())

        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_public_route_fail_open_when_redis_unavailable(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("RATE_LIMIT_FAIL_CLOSED", "false")
        monkeypatch.setenv("RATE_LIMIT_AUTH_FAIL_CLOSED", "true")

        middleware = RateLimitMiddleware(app=AsyncMock())
        request = _make_request(path="/api/products", method="GET")

        downstream = AsyncMock(return_value=MagicMock(status_code=200))
        with patch(
            "src.plugins.rate_limit.redis_client.get_client",
            new_callable=AsyncMock,
            side_effect=RuntimeError("redis down"),
        ):
            response = await middleware.dispatch(request, downstream)

        assert downstream.await_count == 1
        assert response.status_code == 200


class TestRateLimitMiddlewareDoesNotMaskDownstreamErrors:
    """Regression test: previously call_next(request) ran INSIDE the same
    try/except that catches rate-limit-check failures, so ANY unhandled
    exception in the real route handler — not just a Redis/rate-limit
    problem — got relabeled as a generic, unlogged 503 "Rate limit service
    unavailable". That masked a real bug (a Mongo unique-index collision on
    brand-new customer signups) for weeks: the customer's OTP had already
    been consumed by the time the unrelated downstream error fired, so a
    retry then showed a misleading "Invalid or expired OTP" instead of a
    traceable error. call_next must run outside the rate-limit try/except
    so a downstream exception reaches FastAPI's own exception handling
    instead of being swallowed here."""

    @pytest.mark.asyncio
    async def test_downstream_exception_propagates_not_masked_as_503(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("RATE_LIMIT_AUTH_FAIL_CLOSED", "true")

        middleware = RateLimitMiddleware(app=AsyncMock())
        request = _make_request(
            path="/api/auth/otp/verify",
            method="POST",
            headers={"content-type": "application/json"},
        )

        async def receive():
            return {
                "type": "http.request",
                "body": b'{"identifier":"a@b.com","otp":"123456"}',
            }

        request._receive = receive

        # The rate-limit check itself succeeds (real Redis, real checks) —
        # only the actual route handler fails, simulating the real bug
        # (a DuplicateKeyError raised deep inside otp_verify's account
        # creation, after the OTP was already validated/consumed).
        downstream = AsyncMock(side_effect=RuntimeError("simulated downstream account-creation bug"))

        with pytest.raises(RuntimeError, match="simulated downstream account-creation bug"):
            await middleware.dispatch(request, downstream)

        assert downstream.await_count == 1


class TestLoginLockout:
    @pytest.mark.asyncio
    async def test_lockout_after_max_failures(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
        monkeypatch.setenv("LOGIN_MAX_FAILED_ATTEMPTS", "3")
        monkeypatch.setenv("LOGIN_LOCKOUT_SECONDS", "600")
        monkeypatch.setenv("LOGIN_FAILURE_WINDOW_SECONDS", "300")

        service = LoginLockoutService()
        mock_redis = AsyncMock()
        counters = {"ip": 0, "email": 0}

        async def incr(key):
            if ":ip:" in key:
                counters["ip"] += 1
                return counters["ip"]
            counters["email"] += 1
            return counters["email"]

        mock_redis.incr = AsyncMock(side_effect=incr)
        mock_redis.expire = AsyncMock(return_value=True)
        mock_redis.setex = AsyncMock(return_value=True)
        mock_redis.delete = AsyncMock(return_value=1)
        mock_redis.ttl = AsyncMock(return_value=-2)
        mock_redis.pipeline = MagicMock(
            return_value=MagicMock(
                incr=MagicMock(return_value=None),
                expire=MagicMock(return_value=None),
                setex=MagicMock(return_value=None),
                delete=MagicMock(return_value=None),
                execute=AsyncMock(side_effect=[[3, True, 3, True], []]),
            )
        )

        with patch(
            "src.security.login_lockout.redis_client.get_client",
            new_callable=AsyncMock,
            return_value=mock_redis,
        ), patch(
            "src.security.login_lockout.settings.login_max_failed_attempts",
            3,
        ), patch(
            "src.security.login_lockout.settings.login_lockout_seconds",
            600,
        ):
            status = await service.record_failure("203.0.113.1", "admin@test.com")

        assert status.locked is True
        assert status.retry_after_seconds == 600

    @pytest.mark.asyncio
    async def test_assert_not_locked_raises(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")

        service = LoginLockoutService()
        mock_redis = AsyncMock()
        mock_redis.ttl = AsyncMock(return_value=120)
        with patch(
            "src.security.login_lockout.redis_client.get_client",
            new_callable=AsyncMock,
            return_value=mock_redis,
        ):
            with pytest.raises(AccountLockedError) as exc:
                await service.assert_not_locked("203.0.113.1", "admin@test.com")
        assert exc.value.retry_after_seconds == 120


class TestProductionRateLimitGuard:
    def test_production_rejects_disabled_auth_fail_closed(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_example")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "razorpay-live-secret-32chars!")
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://shop.example.com")
        monkeypatch.setenv("JWT_SECRET", "v9K!mQ2@nP7#xR4$wL8%zT1^yU6&hJ3*")
        monkeypatch.setenv("CRON_SECRET", "cron-secret-rotation-32chars!")
        monkeypatch.setenv("METRICS_TOKEN", "metrics-token-rotation-32ch!")
        monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
        monkeypatch.setenv("ADMIN_PASSWORD_HASH", STRONG_ADMIN_HASH)
        monkeypatch.setenv("FRAUD_ENABLED", "true")
        monkeypatch.setenv("IDEMPOTENCY_ENABLED", "true")
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.setenv("RATE_LIMIT_AUTH_FAIL_CLOSED", "false")

        with pytest.raises(ValueError, match="RATE_LIMIT_AUTH_FAIL_CLOSED"):
            Settings(_env_file=None)

    def test_production_rejects_trust_x_forwarded_for(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_example")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "razorpay-live-secret-32chars!")
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://shop.example.com")
        monkeypatch.setenv("JWT_SECRET", "v9K!mQ2@nP7#xR4$wL8%zT1^yU6&hJ3*")
        monkeypatch.setenv("CRON_SECRET", "cron-secret-rotation-32chars!")
        monkeypatch.setenv("METRICS_TOKEN", "metrics-token-rotation-32ch!")
        monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
        monkeypatch.setenv("ADMIN_PASSWORD_HASH", STRONG_ADMIN_HASH)
        monkeypatch.setenv("FRAUD_ENABLED", "true")
        monkeypatch.setenv("IDEMPOTENCY_ENABLED", "true")
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.setenv("TRUST_X_FORWARDED_FOR", "true")

        with pytest.raises(ValueError, match="TRUST_X_FORWARDED_FOR"):
            Settings(_env_file=None)
