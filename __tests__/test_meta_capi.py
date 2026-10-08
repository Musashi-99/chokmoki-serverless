"""Meta Conversions API Purchase — event shape, PII hashing, exactly-once
guard, and the "never break checkout" guarantees."""
import os, sys
os.environ["ENVIRONMENT"] = "development"
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.config import settings
from src.tracking import meta_capi as capi


def _h(v):
    return hashlib.sha256(v.encode()).hexdigest()


def _order(**over):
    doc = {
        "order_id": "7c1f-order",
        "user_id": "u123",
        "user_email": "Buyer@Example.com ",
        "shipping_address": {
            "email": "Buyer@Example.com ", "full_name": "Asha Devi Rao", "phone": "+91 98765-43210",
            "city": "Kolkata", "state": "West Bengal", "postal_code": "700 052", "country": "India",
        },
        "items": [
            {"product_id": "p1", "quantity": 2, "unit_price": 1100.0},
            {"product_id": "p2", "quantity": 1, "unit_price": 825.0},
        ],
        "total_amount": 3025.0,
        "currency": "INR",
        "created_at": datetime.utcnow(),
        "region_audit": {"ip": "49.37.1.2", "user_agent": "Mozilla/5.0 Test", "ip_country": "IN"},
        "tracking": {"fbp": "fb.1.111.222", "fbc": "fb.1.333.abc", "event_source_url": "https://www.chokmoki.com/checkout"},
    }
    doc.update(over)
    return doc


# ---------------------------------------------------------- normalization
def test_sha256_normalizes_case_and_whitespace():
    assert capi.sha256_hex("  Buyer@Example.com ") == _h("buyer@example.com")
    assert capi.sha256_hex("") is None and capi.sha256_hex(None) is None


@pytest.mark.parametrize("phone,country,expected", [
    ("+91 98765-43210", "in", "919876543210"),
    ("98765 43210", "in", "919876543210"),
    ("919876543210", "in", "919876543210"),
    ("0412 345 678", "au", "61412345678"),
    ("", "in", None),
])
def test_phone_normalization(phone, country, expected):
    assert capi.normalize_phone(phone, country) == expected


def test_country_iso_accepts_names_and_codes():
    assert capi.country_iso("India") == "in"
    assert capi.country_iso("AU") == "au"
    assert capi.country_iso("New Zealand") == "nz"
    assert capi.country_iso("Atlantis") is None


# ------------------------------------------------------------ event shape
def test_purchase_event_matches_browser_dedupe_and_hashes_pii():
    ev = capi.build_purchase_event(_order())
    assert ev["event_name"] == "Purchase"
    assert ev["event_id"] == "7c1f-order"  # must equal the browser's eventID
    assert ev["action_source"] == "website"
    assert ev["event_source_url"] == "https://www.chokmoki.com/checkout"
    u = ev["user_data"]
    assert u["em"] == _h("buyer@example.com")
    assert u["ph"] == _h("919876543210")
    assert u["fn"] == _h("asha") and u["ln"] == _h("devi rao")
    assert u["ct"] == _h("kolkata") and u["st"] == _h("westbengal")
    assert u["zp"] == _h("700052") and u["country"] == _h("in")
    assert u["external_id"] == _h("u123")
    # raw, not hashed — Meta requires these unhashed
    assert u["fbp"] == "fb.1.111.222" and u["fbc"] == "fb.1.333.abc"
    assert u["client_ip_address"] == "49.37.1.2" and u["client_user_agent"] == "Mozilla/5.0 Test"
    c = ev["custom_data"]
    assert c["currency"] == "INR" and c["value"] == 3025.0
    assert c["content_ids"] == ["p1", "p2"] and c["num_items"] == 3 and c["order_id"] == "7c1f-order"
    assert c["contents"][0] == {"id": "p1", "quantity": 2, "item_price": 1100.0}
    assert "buyer@example" not in json.dumps(ev)  # no plaintext email anywhere


def test_old_or_missing_created_at_falls_back_to_now():
    now = 2_000_000_000.0
    old = datetime.utcfromtimestamp(now) - timedelta(days=30)
    assert capi.build_purchase_event(_order(created_at=old), now=now)["event_time"] == int(now)
    assert capi.build_purchase_event(_order(created_at=None), now=now)["event_time"] == int(now)


def test_missing_optional_buyer_fields_are_omitted_not_sent_empty():
    ev = capi.build_purchase_event(_order(tracking={}, region_audit={}, user_id=None))
    u = ev["user_data"]
    for k in ("fbp", "fbc", "client_ip_address", "client_user_agent", "external_id"):
        assert k not in u
    assert ev["event_source_url"].endswith("/thank-you")


def test_razorpay_iso_string_created_at_is_accepted():
    ev = capi.build_purchase_event(_order(created_at=datetime.utcnow().isoformat()))
    assert isinstance(ev["event_time"], int)


# ------------------------------------------------------------ send path
@pytest.fixture
def configured():
    with patch.object(settings, "meta_dataset_id", "999"), \
         patch.object(settings, "meta_capi_token", "tok"), \
         patch.object(settings, "meta_test_code", ""):
        yield


@pytest.mark.asyncio
async def test_disabled_without_dataset_or_token():
    with patch.object(settings, "meta_dataset_id", ""), patch.object(settings, "meta_capi_token", ""), \
         patch.object(settings, "fb_catalog_access_token", ""):
        assert await capi.send_purchase(_order()) is False


@pytest.mark.asyncio
async def test_already_claimed_event_is_not_sent_twice(configured):
    post = AsyncMock()
    with patch.object(capi, "_claim", AsyncMock(return_value=False)), patch.object(capi, "_post_events", post):
        assert await capi.send_purchase(_order()) is False
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_sends_once_and_keeps_the_claim(configured):
    post = AsyncMock(return_value={"events_received": 1})
    release = AsyncMock()
    with patch.object(capi, "_claim", AsyncMock(return_value=True)), \
         patch.object(capi, "_post_events", post), patch.object(capi, "_release", release):
        assert await capi.send_purchase(_order()) is True
    post.assert_awaited_once()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_never_raises_releases_claim_and_logs(configured):
    release, log = AsyncMock(), AsyncMock()
    with patch.object(capi, "_claim", AsyncMock(return_value=True)), \
         patch.object(capi, "_post_events", AsyncMock(side_effect=ValueError("bad token"))), \
         patch.object(capi, "_release", release), \
         patch.object(capi.SystemLogService, "log", log):
        assert await capi.send_purchase(_order()) is False
    release.assert_awaited_once()
    assert log.await_args.kwargs["component"] == "meta_capi"


@pytest.mark.asyncio
async def test_transient_errors_are_retried_then_succeed(configured):
    calls = {"n": 0}

    async def flaky(events):
        calls["n"] += 1
        if calls["n"] < 3:
            raise capi._TransientCapiError("HTTP 503")
        return {"events_received": 1}

    with patch.object(capi, "_claim", AsyncMock(return_value=True)), patch.object(capi, "_post_events", flaky), \
         patch("src.resilience.retry.asyncio.sleep", AsyncMock()):
        assert await capi.send_purchase(_order()) is True
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_request_sent_to_meta_has_dataset_token_events_and_test_code(configured):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        return httpx.Response(200, json={"events_received": 1})

    real = httpx.AsyncClient
    with patch.object(settings, "meta_test_code", "TEST46015"), \
         patch.object(capi.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
        body = await capi._post_events([capi.build_purchase_event(_order())])
    assert body["events_received"] == 1
    assert seen["url"].endswith("/999/events")
    assert seen["body"]["test_event_code"] == "TEST46015"
    assert seen["body"]["access_token"] == "tok"
    assert "data" in seen["body"]


@pytest.mark.asyncio
async def test_no_test_code_in_production(configured):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"events_received": 1})

    real = httpx.AsyncClient
    with patch.object(capi.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
        await capi._post_events([{}])
    assert "test_event_code" not in seen["body"]


@pytest.mark.asyncio
async def test_meta_rejection_is_not_retried_as_transient(configured):
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "Invalid OAuth access token"}})

    real = httpx.AsyncClient
    with patch.object(capi.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
        with pytest.raises(ValueError, match="Invalid OAuth"):
            await capi._post_events([{}])
