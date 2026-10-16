"""Public enquiry form: validation rules, spam/abuse handling, and the
product-optional behaviour the walk-in page relies on."""
import os, sys
os.environ["ENVIRONMENT"] = "development"
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.routes import enquiries as route
from src.models.enquiry import EnquiryCreateInput
from src.services.enquiry_service import EnquiryService, csv_safe, public_row


def _payload(**over):
    p = {"name": "Asha Rao", "email": "asha@example.com", "notifyVia": ["email"]}
    p.update(over)
    return p


# ------------------------------------------------------------------ model
def test_minimal_general_enquiry_is_valid_without_a_product():
    m = EnquiryCreateInput(**_payload())
    assert m.productId is None and m.quantity is None and m.size is None


def test_quantity_and_size_are_dropped_without_a_product():
    m = EnquiryCreateInput(**_payload(quantity=3, size="Adjustable"))
    assert m.quantity is None and m.size is None


def test_product_defaults_quantity_to_one_and_keeps_size():
    m = EnquiryCreateInput(**_payload(productId="abc", size="Free size"))
    assert m.quantity == 1 and m.size == "Free size"


def test_blank_product_id_counts_as_no_product():
    m = EnquiryCreateInput(**_payload(productId="   ", quantity=2))
    assert m.productId is None and m.quantity is None


@pytest.mark.parametrize("bad", [0, 11, -1])
def test_quantity_must_be_1_to_10(bad):
    with pytest.raises(ValidationError):
        EnquiryCreateInput(**_payload(productId="abc", quantity=bad))


def test_name_and_email_required_and_not_blank():
    with pytest.raises(ValidationError):
        EnquiryCreateInput(**_payload(name="   "))
    with pytest.raises(ValidationError):
        EnquiryCreateInput(**_payload(email=""))


def test_notify_via_needs_a_valid_channel_and_sms_needs_a_phone():
    with pytest.raises(ValidationError):
        EnquiryCreateInput(**_payload(notifyVia=[]))
    with pytest.raises(ValidationError):
        EnquiryCreateInput(**_payload(notifyVia=["pigeon"]))
    with pytest.raises(ValidationError):
        EnquiryCreateInput(**_payload(notifyVia=["sms"]))
    ok = EnquiryCreateInput(**_payload(notifyVia=["SMS", "email", "sms"], phone="+91 98765 43210"))
    assert ok.notifyVia == ["sms", "email"]


def test_field_length_caps_stop_oversized_payloads():
    with pytest.raises(ValidationError):
        EnquiryCreateInput(**_payload(message="x" * 2001))
    with pytest.raises(ValidationError):
        EnquiryCreateInput(**_payload(name="n" * 121))


def test_honeypot_flags_spam_only_when_filled():
    assert EnquiryCreateInput(**_payload(website="http://spam.test")).is_spam is True
    assert EnquiryCreateInput(**_payload(website="")).is_spam is False
    assert EnquiryCreateInput(**_payload()).is_spam is False


# ------------------------------------------------------------------ route
def _request():
    return SimpleNamespace(headers={"user-agent": "KioskBrowser/1.0"})


@pytest.mark.asyncio
async def test_valid_general_enquiry_is_stored_and_only_the_id_is_returned():
    record = AsyncMock(return_value={"_id": "65f0aa", "ip": "1.2.3.4", "email": "asha@example.com"})
    with patch.object(route, "EnquiryService") as svc, patch.object(route, "get_client_ip", lambda r: "1.2.3.4"):
        svc.return_value.record = record
        resp = await route.api_create_enquiry(_request(), _payload(message="Looking for gifts"))
    assert resp.status_code == 201
    assert json.loads(resp.body) == {"id": "65f0aa"}  # no ip / email / internals leaked
    kwargs = record.await_args.kwargs
    assert kwargs["product_name"] == "" and kwargs["ip"] == "1.2.3.4" and kwargs["user_agent"] == "KioskBrowser/1.0"


@pytest.mark.asyncio
async def test_enquiry_with_a_product_stores_a_name_snapshot():
    product = SimpleNamespace(name="Golden Wing Bee pendants", slug="golden-wing-bee-pendants", active=True)
    record = AsyncMock(return_value={"_id": "x1"})
    with patch.object(route, "ProductService") as ps, patch.object(route, "EnquiryService") as svc, \
         patch.object(route, "get_client_ip", lambda r: None):
        ps.return_value.get_by_id = AsyncMock(return_value=product)
        svc.return_value.record = record
        resp = await route.api_create_enquiry(_request(), _payload(productId="p1", quantity=2))
    assert resp.status_code == 201
    assert record.await_args.kwargs["product_name"] == "Golden Wing Bee pendants"
    assert record.await_args.kwargs["product_slug"] == "golden-wing-bee-pendants"
    assert record.await_args.args[0].quantity == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("product", [None, SimpleNamespace(name="x", slug="x", active=False)])
async def test_unknown_or_inactive_product_is_rejected(product):
    with patch.object(route, "ProductService") as ps, patch.object(route, "EnquiryService") as svc:
        ps.return_value.get_by_id = AsyncMock(return_value=product)
        svc.return_value.record = AsyncMock()
        with pytest.raises(HTTPException) as e:
            await route.api_create_enquiry(_request(), _payload(productId="nope"))
        svc.return_value.record.assert_not_awaited()
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_bots_filling_the_honeypot_get_a_fake_success_and_nothing_is_stored():
    with patch.object(route, "EnquiryService") as svc:
        svc.return_value.record = AsyncMock()
        resp = await route.api_create_enquiry(_request(), _payload(website="http://buy-pills.test"))
        svc.return_value.record.assert_not_awaited()
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_invalid_payload_is_a_422():
    with pytest.raises(HTTPException) as e:
        await route.api_create_enquiry(_request(), {"name": "x"})
    assert e.value.status_code == 422


# ---------------------------------------------------------------- service
def test_search_is_regex_escaped_and_status_filtered():
    q = EnquiryService()._build_query(status="new", search="a.b(c")
    assert q["status"] == "new"
    patterns = [c[next(iter(c))]["$regex"] for c in q["$or"]]
    assert all("\\." in p and "\\(" in p for p in patterns)


def test_no_filters_means_an_empty_query():
    assert EnquiryService()._build_query() == {}


def test_public_row_defaults_a_general_enquiry():
    row = public_row({"_id": 5, "name": "A", "email": "a@b.c", "created_at": None})
    assert row["_id"] == "5" and row["status"] == "new" and row["product_name"] == "" and row["notify_via"] == []


@pytest.mark.parametrize("value,expected", [
    ("=HYPERLINK(\"http://evil\")", "'=HYPERLINK(\"http://evil\")"),
    ("+cmd|' /C calc'!A0", "'+cmd|' /C calc'!A0"),
    ("-2+3", "'-2+3"), ("@SUM(A1)", "'@SUM(A1)"),
    ("normal text", "normal text"), (None, None), (5, 5),
])
def test_csv_cells_cannot_run_as_spreadsheet_formulas(value, expected):
    assert csv_safe(value) == expected
