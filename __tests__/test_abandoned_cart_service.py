"""AbandonedCartService: dedup/merge on email-or-phone, capture-scope
gating (no identifier -> no row), and the order-conversion hook. Uses a
REAL Mongo connection (this project's established pattern for
service-level tests) — see test_admin_order_region_pricing.py."""
from __future__ import annotations

import uuid

import pytest

from src.database.connection import db
from src.models.abandoned_cart import AbandonedCartRecordInput
from src.services.abandoned_cart_service import AbandonedCartService

COLLECTION = "abandoned_carts"


@pytest.fixture(autouse=True)
def _reset_mongo_singleton_per_test():
    db._client = None
    yield
    db._client = None


def _unique_email() -> str:
    return f"lead-{uuid.uuid4().hex[:10]}@example.com"


def _unique_phone() -> str:
    # 10 digits, deterministic per test via uuid tail
    return "9" + uuid.uuid4().hex[:9].replace("a", "1").replace("b", "2").replace(
        "c", "3"
    ).replace("d", "4").replace("e", "5").replace("f", "6")


async def _cleanup(*ids):
    database = await db.get_database()
    if ids:
        await database[COLLECTION].delete_many({"_id": {"$in": list(ids)}})


class TestNoIdentifierNoRow:
    @pytest.mark.asyncio
    async def test_neither_email_nor_phone_records_nothing(self):
        service = AbandonedCartService()
        result = await service.record(AbandonedCartRecordInput(cartItems=[]), ip="1.2.3.4")
        assert result is None

    @pytest.mark.asyncio
    async def test_blank_strings_also_record_nothing(self):
        service = AbandonedCartService()
        result = await service.record(AbandonedCartRecordInput(email="  ", phone=""))
        assert result is None


class TestInsertAndSnapshot:
    @pytest.mark.asyncio
    async def test_first_capture_with_email_creates_a_row_with_full_snapshot(self):
        service = AbandonedCartService()
        email = _unique_email()
        doc = await service.record(
            AbandonedCartRecordInput(
                email=email,
                cartItems=[{
                    "productId": "p1", "productName": "Golden Wing Bee Pendant",
                    "quantity": 2, "price": 500.0, "total": 1000.0,
                    "currency": "INR", "sym": "₹",
                }],
                pricing={"subtotal": 1000.0, "discount": 0, "shipping": 0, "total": 1000.0},
                shippingAddress={"fullName": "Asha Rao", "city": "Mumbai", "country": "India"},
                selectedCountry="IN",
                source="checkout",
            ),
            ip="10.0.0.1",
            user_agent="pytest",
        )
        try:
            assert doc is not None
            assert doc["email_normalized"] == email.lower()
            assert doc["cart_item_count"] == 2
            assert doc["cart_value"] == 1000.0
            assert doc["status"] == "active"
            assert doc["capture_count"] == 1
            assert doc["region"] == "IN"
            assert doc["shipping_address"]["fullName"] == "Asha Rao"
            assert doc["ip"] == "10.0.0.1"
        finally:
            await _cleanup(doc["_id"])

    @pytest.mark.asyncio
    async def test_phone_only_capture_creates_a_row(self):
        service = AbandonedCartService()
        phone = _unique_phone()
        doc = await service.record(AbandonedCartRecordInput(phone=phone))
        try:
            assert doc is not None
            assert doc["phone_normalized"] == phone
            assert doc["email_normalized"] is None
        finally:
            await _cleanup(doc["_id"])


class TestMergeOnRepeatCapture:
    @pytest.mark.asyncio
    async def test_second_capture_same_email_updates_not_duplicates(self):
        service = AbandonedCartService()
        email = _unique_email()
        first = await service.record(AbandonedCartRecordInput(
            email=email, cartItems=[{"productId": "p1", "quantity": 1, "total": 100.0}],
        ))
        second = await service.record(AbandonedCartRecordInput(
            email=email, cartItems=[{"productId": "p1", "quantity": 3, "total": 300.0}],
        ))
        try:
            assert second["_id"] == first["_id"]
            assert second["capture_count"] == 2
            assert second["cart_value"] == 300.0
        finally:
            await _cleanup(first["_id"])

    @pytest.mark.asyncio
    async def test_second_capture_adds_phone_without_losing_email(self):
        service = AbandonedCartService()
        email = _unique_email()
        phone = _unique_phone()
        first = await service.record(AbandonedCartRecordInput(email=email))
        second = await service.record(AbandonedCartRecordInput(email=email, phone=phone))
        try:
            assert second["_id"] == first["_id"]
            assert second["email_normalized"] == email.lower()
            assert second["phone_normalized"] == phone
        finally:
            await _cleanup(first["_id"])

    @pytest.mark.asyncio
    async def test_repeat_capture_with_empty_cart_does_not_blank_earlier_snapshot(self):
        service = AbandonedCartService()
        email = _unique_email()
        first = await service.record(AbandonedCartRecordInput(
            email=email, cartItems=[{"productId": "p1", "quantity": 1, "total": 100.0}],
        ))
        second = await service.record(AbandonedCartRecordInput(email=email, cartItems=[]))
        try:
            assert second["cart_item_count"] == 1
            assert second["cart_value"] == 100.0
        finally:
            await _cleanup(first["_id"])


class TestTwoDocumentMerge:
    @pytest.mark.asyncio
    async def test_email_matches_one_doc_phone_matches_another_merges_into_older(self):
        service = AbandonedCartService()
        email = _unique_email()
        phone = _unique_phone()
        other_email = _unique_email()

        by_email = await service.record(AbandonedCartRecordInput(email=email))
        by_phone = await service.record(AbandonedCartRecordInput(phone=phone, email=other_email))

        # by_email is the older doc (created first) -> merge should survive as by_email's _id
        merged = await service.record(AbandonedCartRecordInput(email=email, phone=phone))

        try:
            assert merged["_id"] == by_email["_id"]
            assert merged["email_normalized"] == email.lower()
            assert merged["phone_normalized"] == phone

            database = await db.get_database()
            remaining = await database[COLLECTION].count_documents(
                {"_id": {"$in": [by_email["_id"], by_phone["_id"]]}}
            )
            assert remaining == 1  # the duplicate (by_phone) was deleted
        finally:
            await _cleanup(by_email["_id"], by_phone["_id"])


class TestMarkConverted:
    @pytest.mark.asyncio
    async def test_matching_active_lead_flips_to_converted(self):
        service = AbandonedCartService()
        email = _unique_email()
        doc = await service.record(AbandonedCartRecordInput(email=email))
        try:
            flipped = await service.mark_converted(email=email, phone=None, order_id="order-123")
            assert flipped == 1

            database = await db.get_database()
            updated = await database[COLLECTION].find_one({"_id": doc["_id"]})
            assert updated["status"] == "converted"
            assert updated["converted_order_id"] == "order-123"
            assert updated["converted_at"] is not None
        finally:
            await _cleanup(doc["_id"])

    @pytest.mark.asyncio
    async def test_no_matching_lead_is_a_silent_no_op(self):
        service = AbandonedCartService()
        flipped = await service.mark_converted(
            email=_unique_email(), phone=None, order_id="order-nope"
        )
        assert flipped == 0

    @pytest.mark.asyncio
    async def test_neither_identifier_is_a_no_op(self):
        service = AbandonedCartService()
        flipped = await service.mark_converted(email=None, phone=None, order_id="x")
        assert flipped == 0


class TestListAndCountFilters:
    @pytest.mark.asyncio
    async def test_search_matches_email_and_count_agrees_with_list(self):
        service = AbandonedCartService()
        email = _unique_email()
        doc = await service.record(AbandonedCartRecordInput(email=email))
        try:
            needle = email.split("@")[0]
            rows = await service.list(search=needle, limit=30)
            count = await service.count(search=needle)
            assert count >= 1
            assert any(r["_id"] == str(doc["_id"]) for r in rows)
        finally:
            await _cleanup(doc["_id"])

    @pytest.mark.asyncio
    async def test_converted_filter_excludes_active_rows(self):
        service = AbandonedCartService()
        email = _unique_email()
        doc = await service.record(AbandonedCartRecordInput(email=email))
        try:
            needle = email.split("@")[0]
            active_rows = await service.list(search=needle, converted=False)
            converted_rows = await service.list(search=needle, converted=True)
            assert any(r["_id"] == str(doc["_id"]) for r in active_rows)
            assert not any(r["_id"] == str(doc["_id"]) for r in converted_rows)
        finally:
            await _cleanup(doc["_id"])
