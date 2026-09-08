from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from src.database.connection import db
from src.models.user import Address, AddressInput, User, UserProfileUpdate


class ProfileConflictError(Exception):
    """Raised when a profile update's phone/email is already claimed by a
    different account — surfaced as a 409, not a raw 500.
    """

    def __init__(self, field: str):
        self.field = field
        super().__init__(f"This {field} is already linked to another account")

COLLECTION_NAME = "users"


def normalize_phone(raw: str) -> str:
    """Normalize to a bare 10-digit Indian mobile number (no +91/spaces/
    dashes) — the same shape shipping_address.phone is stored in on Order
    docs, so lookups against `orders.shipping_address.phone` match directly.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def address_identity(line1: str, postal_code: str) -> tuple[str, str]:
    line = re.sub(r"\s+", " ", (line1 or "").strip().lower())
    pin = re.sub(r"\D", "", postal_code or "")
    return line, pin


def _insert_doc(user: User) -> dict:
    """Doc to insert for a brand-new user — drops fields left at their
    `None` default (phone on an email-only signup, email on a phone-only
    one) instead of writing them out as an explicit `null`.

    This matters because of how the unique indexes below work: `sparse`
    only excludes documents where the field is completely ABSENT, not
    documents where the field is present with value `null` — Mongo still
    indexes an explicit null like any other value. Two phone-less
    signups both writing `phone: null` collide on that index as
    duplicates of each other, even though neither has a phone at all.
    Omitting the key entirely (via exclude_none) is what actually makes a
    phone-less/email-less doc invisible to its sparse unique index.
    """
    return user.model_dump(exclude_none=True)


class UserService:
    async def ensure_indexes(self) -> None:
        database = await db.get_database()
        # partialFilterExpression (not sparse — see _insert_doc's docstring
        # for why sparse alone doesn't work): only documents where the field
        # is actually a string participate in the uniqueness constraint, so
        # any number of phone-only and email-only accounts can coexist
        # without colliding with each other on the field they don't have.
        await database[COLLECTION_NAME].create_index(
            "phone",
            unique=True,
            partialFilterExpression={"phone": {"$type": "string"}},
        )
        await database[COLLECTION_NAME].create_index(
            "email",
            unique=True,
            partialFilterExpression={"email": {"$type": "string"}},
        )

    async def get_by_id(self, user_id: str) -> Optional[User]:
        database = await db.get_database()
        doc = await database[COLLECTION_NAME].find_one({"id": user_id})
        return User(**doc) if doc else None

    async def get_by_phone(self, phone: str) -> Optional[User]:
        database = await db.get_database()
        phone = normalize_phone(phone)
        if not phone:
            return None
        doc = await database[COLLECTION_NAME].find_one({"phone": phone})
        return User(**doc) if doc else None

    async def get_by_email(self, email: str) -> Optional[User]:
        database = await db.get_database()
        email = normalize_email(email)
        if not email:
            return None
        doc = await database[COLLECTION_NAME].find_one({"email": email})
        return User(**doc) if doc else None

    async def get_or_create_by_phone(self, phone: str) -> User:
        phone = normalize_phone(phone)
        database = await db.get_database()
        collection = database[COLLECTION_NAME]
        now = datetime.utcnow()

        existing = await collection.find_one({"phone": phone})
        if existing:
            await collection.update_one(
                {"phone": phone}, {"$set": {"last_login_at": now}}
            )
            existing["last_login_at"] = now
            return User(**existing)

        user = User(phone=phone, last_login_at=now)
        doc = _insert_doc(user)
        try:
            await collection.insert_one(doc)
        except Exception:
            # Concurrent first-login race (double-tap verify) — the unique
            # index on phone is the actual guard; fall back to the doc that
            # won instead of erroring the second request.
            existing = await collection.find_one({"phone": phone})
            if existing:
                return User(**existing)
            raise
        return user

    async def get_or_create_by_email(self, email: str) -> User:
        email = normalize_email(email)
        database = await db.get_database()
        collection = database[COLLECTION_NAME]
        now = datetime.utcnow()

        existing = await collection.find_one({"email": email})
        if existing:
            await collection.update_one(
                {"email": email}, {"$set": {"last_login_at": now}}
            )
            existing["last_login_at"] = now
            return User(**existing)

        user = User(email=email, email_verified=True, phone_verified=False, last_login_at=now)
        doc = _insert_doc(user)
        try:
            await collection.insert_one(doc)
        except Exception:
            # Same concurrent first-login race as get_or_create_by_phone.
            existing = await collection.find_one({"email": email})
            if existing:
                return User(**existing)
            raise
        return user

    async def update_profile(self, user_id: str, update: UserProfileUpdate) -> Optional[User]:
        database = await db.get_database()
        collection = database[COLLECTION_NAME]
        patch = {k: v for k, v in update.model_dump(exclude_unset=True).items()}
        if not patch:
            return await self.get_by_id(user_id)
        if "phone" in patch and patch["phone"]:
            patch["phone"] = normalize_phone(patch["phone"])
            patch["phone_verified"] = False
        if "email" in patch and patch["email"]:
            patch["email"] = normalize_email(patch["email"])
        patch["updated_at"] = datetime.utcnow()
        try:
            await collection.update_one({"id": user_id}, {"$set": patch})
        except DuplicateKeyError as e:
            # e.details["keyPattern"] tells us which unique index (phone_1 or
            # email_1) collided — surfaced so the caller can show "that
            # phone/email is already linked to another account" instead of a
            # generic 500.
            key_pattern = (getattr(e, "details", None) or {}).get("keyPattern") or {}
            field = "phone" if "phone" in key_pattern else "email" if "email" in key_pattern else "field"
            raise ProfileConflictError(field) from e
        return await self.get_by_id(user_id)

    async def add_address(self, user_id: str, payload: AddressInput) -> Optional[User]:
        database = await db.get_database()
        collection = database[COLLECTION_NAME]
        address = Address(**payload.model_dump())
        if address.is_default:
            await collection.update_one(
                {"id": user_id}, {"$set": {"addresses.$[].is_default": False}}
            )
        await collection.update_one(
            {"id": user_id},
            {"$push": {"addresses": address.model_dump()}, "$set": {"updated_at": datetime.utcnow()}},
        )
        return await self.get_by_id(user_id)

    async def update_address(
        self, user_id: str, address_id: str, payload: AddressInput
    ) -> Optional[User]:
        database = await db.get_database()
        collection = database[COLLECTION_NAME]
        address = Address(id=address_id, **payload.model_dump())
        if address.is_default:
            await collection.update_one(
                {"id": user_id}, {"$set": {"addresses.$[].is_default": False}}
            )
        result = await collection.update_one(
            {"id": user_id, "addresses.id": address_id},
            {"$set": {"addresses.$": address.model_dump(), "updated_at": datetime.utcnow()}},
        )
        if result.matched_count == 0:
            return None
        return await self.get_by_id(user_id)

    async def delete_address(self, user_id: str, address_id: str) -> Optional[User]:
        database = await db.get_database()
        collection = database[COLLECTION_NAME]
        await collection.update_one(
            {"id": user_id},
            {"$pull": {"addresses": {"id": address_id}}, "$set": {"updated_at": datetime.utcnow()}},
        )
        return await self.get_by_id(user_id)

    async def capture_from_checkout(
        self,
        *,
        full_name: str,
        phone: str,
        email: str,
        address_line1: str,
        address_line2: str = "",
        city: str = "",
        state: str = "",
        postal_code: str = "",
        country: str = "India",
        existing_user_id: Optional[str] = None,
    ) -> Optional[str]:
        phone_n = normalize_phone(phone)
        if len(phone_n) != 10:
            phone_n = ""
        email_n = normalize_email(email)
        name_n = (full_name or "").strip()

        user: Optional[User] = None
        if existing_user_id:
            user = await self.get_by_id(existing_user_id)
        if user is None and phone_n:
            user = await self.get_by_phone(phone_n)
        if user is None and email_n:
            user = await self.get_by_email(email_n)

        if user is None:
            if not phone_n and not email_n:
                return None
            user = User(
                phone=phone_n or None,
                phone_verified=False,
                email=email_n or None,
                email_verified=False,
                name=name_n or None,
            )
            database = await db.get_database()
            try:
                await database[COLLECTION_NAME].insert_one(_insert_doc(user))
            except DuplicateKeyError:
                user = None
                if phone_n:
                    user = await self.get_by_phone(phone_n)
                if user is None and email_n:
                    user = await self.get_by_email(email_n)
                if user is None:
                    raise

        patch: dict = {}
        if name_n and not (user.name or "").strip():
            patch["name"] = name_n
        if email_n and not (user.email or "").strip():
            claimed = await self.get_by_email(email_n)
            if claimed is None or claimed.id == user.id:
                patch["email"] = email_n
        if phone_n and not (user.phone or "").strip():
            claimed = await self.get_by_phone(phone_n)
            if claimed is None or claimed.id == user.id:
                patch["phone"] = phone_n
        if patch:
            try:
                updated = await self.update_profile(user.id, UserProfileUpdate(**patch))
                if updated:
                    user = updated
            except ProfileConflictError:
                user = await self.get_by_id(user.id) or user

        if address_line1.strip() and postal_code.strip():
            key = address_identity(address_line1, postal_code)
            already = any(
                address_identity(a.address_line1, a.postal_code) == key
                for a in (user.addresses or [])
            )
            if not already:
                await self.add_address(
                    user.id,
                    AddressInput(
                        label="Home",
                        full_name=name_n or user.name or "",
                        phone=phone_n or user.phone or "",
                        address_line1=address_line1.strip(),
                        address_line2=(address_line2 or "").strip(),
                        city=city.strip(),
                        state=state.strip(),
                        postal_code=postal_code.strip(),
                        country=(country or "India").strip() or "India",
                        is_default=not user.addresses,
                    ),
                )

        return user.id
