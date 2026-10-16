"""Enquiry capture (public) + admin listing/export. See src/models/enquiry.py.

Unlike pre-orders there is no (email, product) upsert: an enquiry is a
conversation starter, and several people at one stall may share a contact or
a device, so every submission is its own row."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId

from src.database.connection import db
from src.models.enquiry import Enquiry, EnquiryCreateInput
from src.security.mongo_safe import coerce_safe_string
from src.services.preorder_service import _parse_filter_datetime
from src.utils.contact_normalize import normalize_email
from src.utils.regex_safe import escape_mongo_regex


class EnquiryService:
    COLLECTION_NAME = "enquiries"

    async def _collection(self):
        database = await db.get_database()
        return database[self.COLLECTION_NAME]

    async def ensure_indexes(self) -> None:
        collection = await self._collection()
        await collection.create_index([("created_at", -1)])
        await collection.create_index([("status", 1), ("created_at", -1)])

    async def record(
        self,
        data: EnquiryCreateInput,
        *,
        product_name: str = "",
        product_slug: str = "",
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        email_normalized = normalize_email(data.email)
        if not email_normalized:
            raise ValueError("A valid email is required")

        doc = Enquiry(
            name=data.name.strip(),
            email=data.email.strip(),
            email_normalized=email_normalized,
            phone=data.phone,
            product_id=data.productId,
            product_name=product_name,
            product_slug=product_slug,
            quantity=data.quantity,
            size=data.size,
            notify_via=data.notifyVia,
            message=(data.message or "").strip(),
            ip=ip,
            user_agent=user_agent,
        ).model_dump()
        collection = await self._collection()
        result = await collection.insert_one(doc)
        doc["_id"] = result.inserted_id
        return doc

    def _build_query(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        query: Dict[str, Any] = {}
        status = coerce_safe_string(status, "status") if status is not None else None
        search = coerce_safe_string(search, "search") if search is not None else None
        from_date = coerce_safe_string(from_date, "from_date") if from_date is not None else None
        to_date = coerce_safe_string(to_date, "to_date") if to_date is not None else None

        if status:
            query["status"] = status
        if search:
            safe = escape_mongo_regex(search)
            query["$or"] = [
                {"name": {"$regex": safe, "$options": "i"}},
                {"email": {"$regex": safe, "$options": "i"}},
                {"phone": {"$regex": safe, "$options": "i"}},
                {"product_name": {"$regex": safe, "$options": "i"}},
                {"message": {"$regex": safe, "$options": "i"}},
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
    ) -> List[Dict[str, Any]]:
        collection = await self._collection()
        cursor = (
            collection.find(self._build_query(status, search, from_date, to_date))
            .sort("created_at", -1)
            .skip(max(0, skip))
            .limit(max(1, min(limit, 10_000)))
        )
        return [doc async for doc in cursor]

    async def count(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> int:
        collection = await self._collection()
        return await collection.count_documents(self._build_query(status, search, from_date, to_date))

    async def get_by_id(self, enquiry_id: str):
        if not ObjectId.is_valid(enquiry_id):
            return None
        collection = await self._collection()
        return await collection.find_one({"_id": ObjectId(enquiry_id)})

    async def update_status(self, enquiry_id: str, status: str):
        if not ObjectId.is_valid(enquiry_id):
            return None
        collection = await self._collection()
        result = await collection.update_one(
            {"_id": ObjectId(enquiry_id)},
            {"$set": {"status": status, "updated_at": datetime.utcnow()}},
        )
        if result.matched_count == 0:
            return None
        return await collection.find_one({"_id": ObjectId(enquiry_id)})


def csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection: public free text starting
    with = + - @ (or a tab/CR) would otherwise execute when an admin opens
    the export in Excel or Sheets."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def public_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The admin-list/status-update/export projection."""
    return {
        "_id": str(doc.get("_id")),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "phone": doc.get("phone"),
        "product_id": doc.get("product_id"),
        "product_name": doc.get("product_name") or "",
        "product_slug": doc.get("product_slug") or "",
        "quantity": doc.get("quantity"),
        "size": doc.get("size"),
        "notify_via": doc.get("notify_via") or [],
        "message": doc.get("message") or "",
        "status": doc.get("status") or "new",
        "source": doc.get("source") or "enquiry_page",
        "ip": doc.get("ip"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }
