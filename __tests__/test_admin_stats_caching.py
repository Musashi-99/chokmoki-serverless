"""Regression test for the reported dashboard slowness: admin_get_stats
and admin_list_orders re-ran their Mongo aggregations/queries from scratch
on every single request. Both now cache-aside via the existing
CacheService (Redis-backed, already used elsewhere in this codebase — see
api/routes/storefront.py) with a 60s TTL. Uses the real test Redis
instance (this project's established pattern) rather than mocking
CacheService, so this proves an actual cache hit is served.

Sync test functions, not async — TestClient runs its own internal event
loop per request; mixing that with `await`-ing Motor/Redis calls directly
inside an `@pytest.mark.asyncio` test function binds those clients to a
different (and then closed) loop than the one TestClient's request just
used, producing "RuntimeError: Event loop is closed" (the same class of
bug this session's other test files work around via singleton resets —
here the simpler fix is just not mixing the two calling styles at all;
cache reads/writes go through a short-lived asyncio.run() instead)."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import types

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


def _reset_redis_singleton() -> None:
    from src.database.redis_connection import redis_client as _redis_client

    _redis_client._client = None
    _redis_client._connection_pool = None


def _cache_delete(key: str) -> None:
    from src.services.cache_service import cache as cache_service

    # Reset BEFORE too, not just after — a prior TestClient call may have
    # left the singleton's client bound to TestClient's own internal
    # (still-open, but different) event loop; without this reset, this
    # asyncio.run() call would silently fail (CacheService swallows all
    # exceptions) against that stale client instead of raising, which
    # would otherwise make the bug obvious.
    _reset_redis_singleton()
    asyncio.run(cache_service.delete(key))
    _reset_redis_singleton()


def _cache_get(key: str) -> str | None:
    from src.services.cache_service import cache as cache_service

    _reset_redis_singleton()
    result = asyncio.run(cache_service.get(key))
    _reset_redis_singleton()
    return result


def _cache_set(key: str, value: str, ttl: int) -> None:
    from src.services.cache_service import cache as cache_service

    _reset_redis_singleton()
    asyncio.run(cache_service.set(key, value, ttl))
    _reset_redis_singleton()


@pytest.fixture
def api_module(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("MONGODB_URI", "mongodb://chokmoki-test-mongo:27017/chokmoki_final3")
    monkeypatch.setenv("REDIS_URL", "redis://chokmoki-test-redis:6379/0")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    _stub_external_modules()
    if "api.index" in sys.modules:
        del sys.modules["api.index"]
    module = importlib.import_module("api.index")
    from src.config import settings as _settings

    monkeypatch.setattr(_settings, "rate_limit_enabled", False)
    yield module

    _reset_redis_singleton()
    from src.database.connection import db as _db

    _db._client = None


def _principal():
    from src.models.admin_auth import AdminPrincipal

    return AdminPrincipal(
        email="root@chokmoki.com",
        role="super_admin",
        session_id="test-session",
        jti="test-jti",
        scopes=frozenset({"*"}),
        regions=frozenset(),
        is_root=True,
    )


def _override(api_module, principal):
    from src.plugins.admin_deps import resolve_admin_principal

    api_module.app.dependency_overrides[resolve_admin_principal] = lambda: principal


def _clear_override(api_module):
    from src.plugins.admin_deps import resolve_admin_principal

    api_module.app.dependency_overrides.pop(resolve_admin_principal, None)


class TestAdminStatsCaching:
    def test_first_stats_request_populates_the_cache_key(self, api_module):
        _cache_delete("admin:stats:all")
        principal = _principal()
        _override(api_module, principal)
        try:
            client = TestClient(api_module.app, raise_server_exceptions=True)
            response = client.get("/api/admin/stats")
            assert response.status_code == 200

            cached = _cache_get("admin:stats:all")
            assert cached is not None
            assert json.loads(cached)["totalOrders"] == response.json()["totalOrders"]
        finally:
            _clear_override(api_module)
            _cache_delete("admin:stats:all")

    def test_stats_request_is_served_from_a_pre_seeded_cache_entry(self, api_module):
        """Seed the cache with a made-up sentinel value first — a real
        cache HIT returns it unchanged; a cache MISS would recompute from
        Mongo and never produce this exact made-up number, proving the
        endpoint actually reads from cache rather than always
        recomputing."""
        _cache_set("admin:stats:all", json.dumps({
            "totalOrders": 999999, "totalRevenue": 0, "ordersToday": 0,
            "statusCounts": {}, "totalProducts": 0, "activeProducts": 0,
        }), 60)
        principal = _principal()
        _override(api_module, principal)
        try:
            client = TestClient(api_module.app, raise_server_exceptions=True)
            response = client.get("/api/admin/stats")
            assert response.status_code == 200
            assert response.json()["totalOrders"] == 999999
        finally:
            _clear_override(api_module)
            _cache_delete("admin:stats:all")
