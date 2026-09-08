"""Regression test for the "second new signup fails with Invalid or expired
OTP" bug: real MongoDB sparse indexes only exclude documents where the
field is completely ABSENT, not documents where it's present with value
`null`. The old `sparse=True` unique index on `users.phone` meant the
FIRST email-only signup (writing `phone: null`) silently claimed that slot,
and every subsequent email-only signup's `insert_one` then raised a real
DuplicateKeyError on the phone index — even though the colliding accounts
have completely different emails and neither has a phone at all.

Uses a REAL Mongo connection (via conftest.py's MONGODB_URI/ENVIRONMENT
env), not a mock — this is fundamentally about actual index semantics that
an in-memory fake collection (see __tests__/test_user_checkout_capture.py's
FakeUsers, which never reproduced this because its duplicate check ignores
falsy fields) cannot exercise or catch.
"""
from __future__ import annotations

import uuid

import pytest

from src.database.connection import db
from src.services.user_service import UserService

COLLECTION_NAME = "users"


@pytest.fixture(autouse=True)
def _reset_mongo_singleton_per_test():
    """pytest-asyncio's default function-scoped event loop means a Motor
    client cached on the module-level MongoSingleton from an earlier test
    (in this file or an earlier-run file in the same session) is bound to
    an already-closed loop by the time this test's own loop runs — reset
    it so each test connects fresh. (Same fix as
    scripts/smoke_test_region_scoping.py's `_run_async` needed for the
    same reason.)"""
    db._client = None
    yield


def _unique_email() -> str:
    return f"regression-{uuid.uuid4().hex[:12]}@example.com"


def _unique_phone() -> str:
    # normalize_phone strips to 10 digits — use a random-looking 10-digit string.
    return str(9000000000 + (uuid.uuid4().int % 999999))


@pytest.mark.asyncio
async def test_two_consecutive_email_only_signups_both_succeed():
    service = UserService()
    await service.ensure_indexes()

    email_a = _unique_email()
    email_b = _unique_email()
    database = await db.get_database()

    try:
        user_a = await service.get_or_create_by_email(email_a)
        user_b = await service.get_or_create_by_email(email_b)

        assert user_a.email == email_a
        assert user_b.email == email_b
        assert user_a.phone is None
        assert user_b.phone is None
        assert user_a.id != user_b.id

        # The bug's exact symptom: a SECOND email-only signup must not raise
        # (previously: DuplicateKeyError on the phone index, surfaced to the
        # customer as a misleading "Invalid or expired OTP").
    finally:
        await database[COLLECTION_NAME].delete_many({"email": {"$in": [email_a, email_b]}})


@pytest.mark.asyncio
async def test_two_consecutive_phone_only_signups_both_succeed():
    """Mirror of the email case — a phone-only signup must not collide on
    the email index either."""
    service = UserService()
    await service.ensure_indexes()

    phone_a = _unique_phone()
    phone_b = _unique_phone()
    database = await db.get_database()

    try:
        user_a = await service.get_or_create_by_phone(phone_a)
        user_b = await service.get_or_create_by_phone(phone_b)

        assert user_a.phone == phone_a
        assert user_b.phone == phone_b
        assert user_a.email is None
        assert user_b.email is None
        assert user_a.id != user_b.id
    finally:
        await database[COLLECTION_NAME].delete_many({"phone": {"$in": [phone_a, phone_b]}})


@pytest.mark.asyncio
async def test_new_user_document_omits_unset_identifier_field():
    """The actual mechanism of the fix: a freshly-inserted email-only doc
    must not have a `phone` key at all (not even `null`) — that's what
    makes the partial index correctly exclude it."""
    service = UserService()
    await service.ensure_indexes()

    email = _unique_email()
    database = await db.get_database()
    try:
        await service.get_or_create_by_email(email)
        raw = await database[COLLECTION_NAME].find_one({"email": email})
        assert raw is not None
        assert "phone" not in raw
    finally:
        await database[COLLECTION_NAME].delete_many({"email": email})


@pytest.mark.asyncio
async def test_users_phone_index_is_partial_not_sparse():
    """Guards the index definition itself — if someone reverts to
    sparse=True this test catches it even before the behavioral tests
    above would notice (e.g. if a stale null document is already present)."""
    service = UserService()
    await service.ensure_indexes()
    database = await db.get_database()
    indexes = await database[COLLECTION_NAME].index_information()

    phone_index = indexes.get("phone_1")
    email_index = indexes.get("email_1")
    assert phone_index is not None
    assert email_index is not None
    assert phone_index.get("partialFilterExpression") == {"phone": {"$type": "string"}}
    assert email_index.get("partialFilterExpression") == {"email": {"$type": "string"}}
    assert not phone_index.get("sparse")
    assert not email_index.get("sparse")
