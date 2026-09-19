"""Abandoned-cart / guest-lead capture, dedup and admin listing.

One document per *person*, not per keystroke: the public capture endpoint
fires repeatedly while someone fills in checkout, so `record()` is an
upsert keyed on whichever contact identifiers we have, and every later
capture merges the newest snapshot onto the same row.

Merging is the whole design problem. A guest typically types their email
first (creating doc A), then their phone. If they'd previously abandoned a
cart using only that phone (doc B), the second capture matches *two*
different documents — so the indexes are sparse but NOT unique, and
`record()` folds the newer document into the older one instead of letting
a unique index throw halfway through a legitimate merge.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from src.database.connection import db
from src.models.abandoned_cart import AbandonedCartRecordInput
from src.models.region import normalize_region_code
from src.plugins.logger import logger
from src.security.mongo_safe import coerce_safe_string
from src.utils.contact_normalize import normalize_email, normalize_phone
from src.utils.money import money
from src.utils.regex_safe import escape_mongo_regex
from src.utils.region import is_au_address, is_india_address, is_nz_address

MAX_CART_ITEMS = 50


class AbandonedCartService:
    COLLECTION_NAME = "abandoned_carts"

    async def _collection(self):
        database = await db.get_database()
        return database[self.COLLECTION_NAME]

    async def ensure_indexes(self) -> None:
        """Sparse, non-unique on both dedup keys — see module docstring for
        why uniqueness would break the two-document merge case. The
        compound (status, last_seen_at) index serves the admin list's
        default "active, newest first" query."""
        collection = await self._collection()
        await collection.create_index("email_normalized", sparse=True)
        await collection.create_index("phone_normalized", sparse=True)
        await collection.create_index([("status", 1), ("last_seen_at", -1)])

    # ---- capture --------------------------------------------------------

    @staticmethod
    def _region_for(
        shipping_country: Optional[str], selected_country: Optional[str]
    ) -> str:
        """The physical destination wins when it's one of the tax regions —
        same string sets as invoice_service's tax-region check, so an
        abandoned cart is bucketed the way its order would have been. The
        storefront's selected market is only a fallback for the (common)
        case where no country has been typed yet."""
        if is_india_address(shipping_country):
            return "IN"
        if is_au_address(shipping_country):
            return "AU"
        if is_nz_address(shipping_country):
            return "NZ"
        if shipping_country and shipping_country.strip():
            return "default"
        return normalize_region_code(selected_country) or "default"

    @staticmethod
    def _snapshot(data: AbandonedCartRecordInput) -> Dict[str, Any]:
        """The 'latest wins' half of a merge: everything derived purely
        from this capture, with no reference to whatever is already stored."""
        items = [item.model_dump() for item in (data.cartItems or [])][:MAX_CART_ITEMS]
        address = data.shippingAddress.model_dump() if data.shippingAddress else {}
        first = items[0] if items else {}
        cart_value = money(sum(float(i.get("total") or 0) for i in items))
        return {
            "cart_items": items,
            "cart_item_count": sum(int(i.get("quantity") or 1) for i in items),
            "cart_value": cart_value,
            "pricing": data.pricing.model_dump() if data.pricing else None,
            "shipping_address": address,
            "coupon_code": data.couponCode,
            "selected_country": data.selectedCountry,
            "region": AbandonedCartService._region_for(
                address.get("country"), data.selectedCountry
            ),
            "currency": first.get("currency"),
            "currency_symbol": first.get("sym"),
            "session_id": data.sessionId,
            "source": data.source,
        }

    async def record(
        self,
        data: AbandonedCartRecordInput,
        *,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Insert or merge one capture. Returns the stored document, or
        None when there is no usable contact identifier (nothing is
        written in that case — an unreachable lead is not worth a row)."""
        email_normalized = normalize_email(data.email)
        phone_normalized = normalize_phone(data.phone)
        if not email_normalized and not phone_normalized:
            return None

        collection = await self._collection()
        now = datetime.utcnow()
        snapshot = self._snapshot(data)

        or_clauses: List[Dict[str, Any]] = []
        if email_normalized:
            or_clauses.append({"email_normalized": email_normalized})
        if phone_normalized:
            or_clauses.append({"phone_normalized": phone_normalized})

        matches = await collection.find({"$or": or_clauses}).sort("first_captured_at", 1).to_list(
            length=10
        )

        if not matches:
            doc: Dict[str, Any] = {
                "email_normalized": email_normalized,
                "phone_normalized": phone_normalized,
                "email_raw": (data.email or "").strip() or None,
                "phone_raw": (data.phone or "").strip() or None,
                "ip": ip,
                "user_agent": user_agent,
                "status": "active",
                "capture_count": 1,
                "first_captured_at": now,
                "last_seen_at": now,
                "converted_at": None,
                "converted_order_id": None,
                **snapshot,
            }
            result = await collection.insert_one(doc)
            doc["_id"] = result.inserted_id
            return doc

        # Oldest surviving document wins — it owns the real
        # first_captured_at, which is the field an admin reads as "when did
        # we first hear from this person".
        survivor = matches[0]
        duplicates = matches[1:]

        updates: Dict[str, Any] = dict(snapshot)
        updates["last_seen_at"] = now
        updates["status"] = survivor.get("status") or "active"
        # Never overwrite a known identifier with a null: a capture fired
        # from a form where the customer had only retyped their phone must
        # not wipe the email we already learned.
        if email_normalized:
            updates["email_normalized"] = email_normalized
            updates["email_raw"] = (data.email or "").strip() or survivor.get("email_raw")
        if phone_normalized:
            updates["phone_normalized"] = phone_normalized
            updates["phone_raw"] = (data.phone or "").strip() or survivor.get("phone_raw")
        if ip:
            updates["ip"] = ip
        if user_agent:
            updates["user_agent"] = user_agent
        # Same rule for the snapshot: an empty cart/address mid-typing
        # shouldn't blank out a richer earlier snapshot.
        if not updates.get("cart_items"):
            for key in ("cart_items", "cart_item_count", "cart_value", "pricing"):
                updates.pop(key, None)
        for key in ("coupon_code", "selected_country", "session_id", "source", "currency",
                    "currency_symbol"):
            if updates.get(key) is None and survivor.get(key) is not None:
                updates.pop(key)
        if not updates.get("shipping_address"):
            updates.pop("shipping_address", None)
        # Carry over any identifier the losing duplicates knew that this
        # capture and the survivor don't, before they're deleted.
        for dup in duplicates:
            for key in ("email_normalized", "email_raw", "phone_normalized", "phone_raw"):
                if not updates.get(key) and not survivor.get(key) and dup.get(key):
                    updates[key] = dup[key]

        await collection.update_one(
            {"_id": survivor["_id"]},
            {"$set": updates, "$inc": {"capture_count": 1}},
        )
        if duplicates:
            await collection.delete_many({"_id": {"$in": [d["_id"] for d in duplicates]}})
            logger.info(
                f"Merged {len(duplicates)} duplicate abandoned cart(s) into {survivor['_id']}"
            )
        return await collection.find_one({"_id": survivor["_id"]})

    async def mark_converted(
        self,
        email: Optional[str],
        phone: Optional[str],
        order_id: str,
    ) -> int:
        """Flip any still-active lead matching this customer to converted.

        Best-effort by nature: most orders never had an abandoned-cart row
        (the customer completed checkout in one go), so matching nothing is
        the normal case, not a failure. Returns the number of rows flipped.
        """
        email_normalized = normalize_email(email)
        phone_normalized = normalize_phone(phone)
        if not email_normalized and not phone_normalized:
            return 0

        or_clauses: List[Dict[str, Any]] = []
        if email_normalized:
            or_clauses.append({"email_normalized": email_normalized})
        if phone_normalized:
            or_clauses.append({"phone_normalized": phone_normalized})

        collection = await self._collection()
        result = await collection.update_many(
            {"$or": or_clauses, "status": "active"},
            {
                "$set": {
                    "status": "converted",
                    "converted_at": datetime.utcnow(),
                    "converted_order_id": order_id,
                }
            },
        )
        return result.modified_count

    # ---- admin listing --------------------------------------------------

    def _build_query(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        country: Optional[str] = None,
        converted: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Mirrors OrderService._build_order_query's shape deliberately —
        same coercion, same regex escaping, same date parsing — so the two
        admin lists can't drift in how a filter behaves."""
        query: Dict[str, Any] = {}
        if status is not None:
            status = coerce_safe_string(status, "status")
        if search is not None:
            search = coerce_safe_string(search, "search")
        if from_date is not None:
            from_date = coerce_safe_string(from_date, "from_date")
        if to_date is not None:
            to_date = coerce_safe_string(to_date, "to_date")
        if country is not None:
            country = coerce_safe_string(country, "country")

        if status:
            query["status"] = status
        if converted is not None:
            # A convenience filter layered over `status` — an explicit
            # status filter still wins if both are sent.
            query.setdefault("status", "converted" if converted else {"$ne": "converted"})
        if country:
            query["region"] = (
                country.strip().upper() if country.strip().lower() != "default" else "default"
            )
        if search:
            safe = escape_mongo_regex(search)
            query["$or"] = [
                {"email_raw": {"$regex": safe, "$options": "i"}},
                {"email_normalized": {"$regex": safe, "$options": "i"}},
                {"phone_raw": {"$regex": safe, "$options": "i"}},
                {"phone_normalized": {"$regex": safe, "$options": "i"}},
                {"shipping_address.fullName": {"$regex": safe, "$options": "i"}},
                {"converted_order_id": {"$regex": safe, "$options": "i"}},
            ]
        if from_date or to_date:
            date_filter: Dict[str, Any] = {}
            if from_date:
                date_filter["$gte"] = _parse_filter_datetime(from_date, end_of_day=False)
            if to_date:
                date_filter["$lte"] = _parse_filter_datetime(to_date, end_of_day=True)
            if date_filter:
                query["last_seen_at"] = date_filter
        return query

    async def list(
        self,
        skip: int = 0,
        limit: int = 30,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        country: Optional[str] = None,
        converted: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        collection = await self._collection()
        query = self._build_query(status, search, from_date, to_date, country, converted)
        cursor = (
            collection.find(query).sort("last_seen_at", -1).skip(max(0, skip)).limit(max(1, limit))
        )
        return [_public_row(doc) async for doc in cursor]

    async def count(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        country: Optional[str] = None,
        converted: Optional[bool] = None,
    ) -> int:
        collection = await self._collection()
        query = self._build_query(status, search, from_date, to_date, country, converted)
        return await collection.count_documents(query)


def _parse_filter_datetime(value: str, *, end_of_day: bool = False) -> datetime:
    """Same YYYY-MM-DD-or-ISO handling as
    OrderService._parse_filter_datetime — kept identical so the two admin
    date filters accept exactly the same inputs."""
    raw = value.strip()
    if "T" in raw or raw.endswith("Z"):
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    day = datetime.fromisoformat(raw)
    if end_of_day:
        return day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def _public_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The admin-list projection — exactly the contracted fields, nothing
    more (ip/user_agent stay internal)."""
    return {
        "_id": str(doc.get("_id")),
        "email_raw": doc.get("email_raw"),
        "phone_raw": doc.get("phone_raw"),
        "cart_items": doc.get("cart_items") or [],
        "cart_item_count": doc.get("cart_item_count") or 0,
        "cart_value": doc.get("cart_value") or 0,
        "currency": doc.get("currency"),
        "currency_symbol": doc.get("currency_symbol"),
        "shipping_address": doc.get("shipping_address") or {},
        "selected_country": doc.get("selected_country"),
        "region": doc.get("region") or "default",
        "status": doc.get("status") or "active",
        "first_captured_at": doc.get("first_captured_at"),
        "last_seen_at": doc.get("last_seen_at"),
        "converted_at": doc.get("converted_at"),
        "converted_order_id": doc.get("converted_order_id"),
    }
