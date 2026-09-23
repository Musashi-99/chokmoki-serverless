"""Pre-order capture (public) + admin listing/export.

Australia is being switched to pre-order-only (a separate, already-planned
frontend change — this service only owns the backend contract). Region is
never hardcoded to "AU": it is resolved through the exact same
selected-country + GeoIP chain used by checkout (src/pricing/resolvers.py's
resolve_country(), fed by src/pricing/geo_provider.py's
GeoIPDiscoveryAdapter — see OrderService._resolve_region for the
precedent), so a client can never write an arbitrary region string
directly onto a stored pre-order.

One document per (email, product) pair — a customer resubmitting the form
for the same product (new quantity/size/message, or just "did that go
through?") updates their existing row rather than creating a duplicate or
erroring. See `record()` for the upsert semantics and why `status` is only
ever set on first insert, never reset on resubmission.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from src.database.connection import db
from src.models.preorder import PreorderCreateInput
from src.models.region import normalize_region_code
from src.pricing.geo_provider import GeoIPDiscoveryAdapter
from src.pricing.resolvers import resolve_country
from src.security.mongo_safe import coerce_safe_string
from src.utils.contact_normalize import normalize_email
from src.utils.regex_safe import escape_mongo_regex


class PreorderService:
    COLLECTION_NAME = "preorders"

    async def _collection(self):
        database = await db.get_database()
        return database[self.COLLECTION_NAME]

    async def ensure_indexes(self) -> None:
        """Unique on (email_normalized, product_id) — the dedup key that
        makes resubmission an update rather than a duplicate row (see
        record()). Compound (region, status, created_at) serves the admin
        list's default region-scoped, status-filtered, newest-first query
        shape — deliberately NOT copying orders' thinner index set (that
        was flagged elsewhere as under-indexed for its own query patterns);
        this mirrors abandoned_cart_service.py's better-indexed model
        instead."""
        collection = await self._collection()
        await collection.create_index(
            [("email_normalized", 1), ("product_id", 1)], unique=True
        )
        await collection.create_index([("region", 1), ("status", 1), ("created_at", -1)])

    # ---- region resolution -----------------------------------------------

    @staticmethod
    async def resolve_region(selected_country: Optional[str], ip: Optional[str]) -> str:
        """Server-side region resolution — identical chain to
        OrderService._resolve_region (selected country > GeoIP > "default"),
        then normalized via normalize_region_code() so the stored value is
        always one of the codes src/models/region.py knows about (or
        "default"). A client's `selectedCountry` is only ever a *candidate*
        here, never trusted directly — resolve_country() only accepts it
        when it's one of the actually-supported market countries."""
        geo = await GeoIPDiscoveryAdapter().lookup(ip or "")
        selected = (selected_country or "").strip().upper() or None
        effective = resolve_country(selected_country=selected, ip_country=geo.country)
        return normalize_region_code(effective) or "default"

    # ---- create / upsert --------------------------------------------------

    async def record(
        self,
        data: PreorderCreateInput,
        *,
        product_name: str,
        product_slug: str,
        region: str,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upsert on (email_normalized, product_id). A resubmission updates
        the mutable fields (quantity/size/notify_via/message/phone/name)
        and bumps `updated_at`, but deliberately does NOT reset `status`
        back to "new" — `status` is only ever set via $setOnInsert, i.e.
        only on the very first submission for that (email, product) pair.
        Rationale: if an admin already marked a lead "notified" or
        "converted" (or archived it), a customer re-submitting the same
        form days later — a very plausible "did that go through?" retry —
        must not silently undo that admin's prior work by bouncing the row
        back to "new"."""
        email_normalized = normalize_email(data.email)
        if not email_normalized:
            raise ValueError("A valid email is required")

        now = datetime.utcnow()
        collection = await self._collection()

        mutable_fields: Dict[str, Any] = {
            "product_id": data.productId,
            "product_name": product_name,
            "product_slug": product_slug,
            "region": region,
            "name": data.name.strip(),
            "email": data.email.strip(),
            "email_normalized": email_normalized,
            "phone": (data.phone or "").strip() or None,
            "quantity": data.quantity,
            "size": data.size,
            "notify_via": data.notifyVia,
            "message": (data.message or "").strip(),
            "ip": ip,
            "user_agent": user_agent,
            "updated_at": now,
        }

        await collection.update_one(
            {"email_normalized": email_normalized, "product_id": data.productId},
            {
                "$set": mutable_fields,
                "$setOnInsert": {"status": "new", "created_at": now},
            },
            upsert=True,
        )
        doc = await collection.find_one(
            {"email_normalized": email_normalized, "product_id": data.productId}
        )
        return doc

    # ---- admin listing / status update ------------------------------------

    def _build_query(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        region: Optional[str] = None,
        regions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Mirrors OrderService._build_order_query / AbandonedCartService.
        _build_query's shape — same coercion, same regex escaping, same
        date parsing."""
        query: Dict[str, Any] = {}
        if status is not None:
            status = coerce_safe_string(status, "status")
        if search is not None:
            search = coerce_safe_string(search, "search")
        if from_date is not None:
            from_date = coerce_safe_string(from_date, "from_date")
        if to_date is not None:
            to_date = coerce_safe_string(to_date, "to_date")
        if region is not None:
            region = coerce_safe_string(region, "region")

        if status:
            query["status"] = status

        # `regions` (a region-scoped admin's forced $in filter) always wins
        # over a bare `region` — mirrors admin_orders.py's countries-takes-
        # precedence-over-country shape.
        if regions:
            query["region"] = {"$in": regions}
        elif region:
            query["region"] = (
                region.strip().upper() if region.strip().lower() != "default" else "default"
            )

        if search:
            safe = escape_mongo_regex(search)
            query["$or"] = [
                {"name": {"$regex": safe, "$options": "i"}},
                {"email": {"$regex": safe, "$options": "i"}},
                {"email_normalized": {"$regex": safe, "$options": "i"}},
                {"phone": {"$regex": safe, "$options": "i"}},
                {"product_name": {"$regex": safe, "$options": "i"}},
            ]
        if from_date or to_date:
            date_filter: Dict[str, Any] = {}
            if from_date:
                date_filter["$gte"] = _parse_filter_datetime(from_date, end_of_day=False)
            if to_date:
                date_filter["$lte"] = _parse_filter_datetime(to_date, end_of_day=True)
            if date_filter:
                query["created_at"] = date_filter
        return query

    async def list(
        self,
        skip: int = 0,
        limit: int = 50,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        region: Optional[str] = None,
        regions: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        collection = await self._collection()
        query = self._build_query(status, search, from_date, to_date, region, regions)
        cursor = (
            collection.find(query)
            .sort("created_at", -1)
            .skip(max(0, skip))
            .limit(max(1, min(limit, 200)))
        )
        return [doc async for doc in cursor]

    async def count(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        region: Optional[str] = None,
        regions: Optional[List[str]] = None,
    ) -> int:
        collection = await self._collection()
        query = self._build_query(status, search, from_date, to_date, region, regions)
        return await collection.count_documents(query)

    async def get_by_id(self, preorder_id: str):
        from bson import ObjectId

        if not ObjectId.is_valid(preorder_id):
            return None
        collection = await self._collection()
        return await collection.find_one({"_id": ObjectId(preorder_id)})

    async def update_status(self, preorder_id: str, status: str, actor: Optional[str] = None):
        from bson import ObjectId

        if not ObjectId.is_valid(preorder_id):
            return None
        collection = await self._collection()
        result = await collection.update_one(
            {"_id": ObjectId(preorder_id)},
            {"$set": {"status": status, "updated_at": datetime.utcnow()}},
        )
        if result.matched_count == 0:
            return None
        return await collection.find_one({"_id": ObjectId(preorder_id)})


def _parse_filter_datetime(value: str, *, end_of_day: bool = False) -> datetime:
    """Same YYYY-MM-DD-or-ISO handling as OrderService/AbandonedCartService's
    equivalents — kept identical so all admin date filters accept exactly
    the same inputs."""
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


def public_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The admin-list/status-update/export projection. Despite the name
    (kept for parity with abandoned_cart_service.py's _public_row, and
    because this function only actually feeds admin routes — the public
    POST /api/preorders response never calls it, it just returns
    {id, status}), it now DOES include `ip` — an admin needs it to
    diagnose exactly the class of bug that motivated this: a submission
    whose stored `region` doesn't match what the customer says they
    selected. `user_agent` stays out; there's no established admin need
    for it yet and it's not what actually helps diagnose a region
    mismatch."""
    return {
        "_id": str(doc.get("_id")),
        "product_id": doc.get("product_id"),
        "product_name": doc.get("product_name"),
        "product_slug": doc.get("product_slug"),
        "region": doc.get("region") or "default",
        "name": doc.get("name"),
        "email": doc.get("email"),
        "phone": doc.get("phone"),
        "quantity": doc.get("quantity"),
        "size": doc.get("size"),
        "notify_via": doc.get("notify_via") or [],
        "message": doc.get("message") or "",
        "status": doc.get("status") or "new",
        "ip": doc.get("ip"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
