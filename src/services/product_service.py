from typing import List, Optional, Dict, Any
from datetime import datetime
from bson import ObjectId
from src.config import settings
from src.database.connection import db
from src.models.product import JewelryProduct, JewelryProductCreate
from src.plugins.logger import logger
from src.utils.regex_safe import escape_mongo_regex
from src.services.product_filters import ids_mongo_filter, merge_mongo_filters
from src.services.stock_alerts import evaluate_stock_crossing

# Optional alerts import
try:
    from src.alerts.events import (
        EVENT_PRODUCT_PRICE_CHANGED,
        event_type_for_stock_crossing,
        publish_alert,
    )
except ImportError:
    EVENT_PRODUCT_PRICE_CHANGED = "product.price_changed"
    event_type_for_stock_crossing = None
    publish_alert = None


def _diff_market_rows(
    before_rows: List[Dict[str, Any]], after_rows: List[Dict[str, Any]], fields: tuple
) -> List[Dict[str, Any]]:
    """Per-country diff of MarketPrice/MarketStock rows — used to build the
    "who changed what, in which region" Telegram alert. Returns one entry
    per country whose tracked `fields` actually differ (a country present
    only in `after` counts as changed from None)."""
    before_by_country = {row.get("country"): row for row in before_rows}
    changes: List[Dict[str, Any]] = []
    for row in after_rows:
        country = row.get("country")
        old_row = before_by_country.get(country)
        old_values = {f: (old_row.get(f) if old_row else None) for f in fields}
        new_values = {f: row.get(f) for f in fields}
        if old_values != new_values:
            changes.append({"country": country, "old": old_values, "new": new_values})
    return changes


# Fields diffed as simple scalar changes in the "Product Updated" alert —
# everything else in an update payload (prices/stock get their own richer
# per-country diff; slug/category etc. are simple enough to show as-is;
# long free-text fields are truncated so the alert stays readable).
_GENERAL_DIFF_FIELDS = {
    "name", "slug", "category", "collection", "active", "is_best_seller", "is_curated",
    "best_seller_order", "curated_order", "purity", "material",
}
_TRUNCATE_LEN = 60


def _format_scalar(value: Any) -> str:
    text = str(value)
    return text if len(text) <= _TRUNCATE_LEN else text[:_TRUNCATE_LEN] + "…"


def _diff_general_fields(before: Dict[str, Any], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Everything in `payload` other than prices/stock/price_inr (those get
    their own per-country diff). Scalar fields show old→new; anything else
    (gallery, sizes, long text fields) just reports "changed" so the alert
    stays short and doesn't leak/duplicate large content into Telegram."""
    changes: List[Dict[str, Any]] = []
    for field, new_value in payload.items():
        if field in {"prices", "stock", "price_inr"}:
            continue
        old_value = before.get(field)
        if old_value == new_value:
            continue
        if field in _GENERAL_DIFF_FIELDS:
            changes.append({
                "field": field,
                "old": _format_scalar(old_value),
                "new": _format_scalar(new_value),
            })
        else:
            changes.append({"field": field, "old": None, "new": None})
    return changes


class ProductService:
    COLLECTION_NAME = "products"

    async def _collection(self):
        database = await db.get_database()
        return database[self.COLLECTION_NAME]

    async def _resolve_filter(self, product_id: str) -> Optional[Dict[str, Any]]:
        """Resolve a Mongo filter from an ObjectId string or product slug."""
        collection = await self._collection()
        product_id = str(product_id).strip()
        if not product_id:
            return None

        if ObjectId.is_valid(product_id):
            filt = {"_id": ObjectId(product_id)}
            if await collection.find_one(filt, {"_id": 1}):
                return filt

        doc = await collection.find_one({"slug": product_id}, {"_id": 1})
        if doc:
            return {"_id": doc["_id"]}
        return None
    
    async def create(self, product_data: JewelryProductCreate) -> JewelryProduct:
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]

        existing = await collection.find_one({"slug": product_data.slug})
        if existing:
            raise ValueError(f"Product with slug '{product_data.slug}' already exists")

        product_dict = product_data.model_dump()
        if product_dict.get("category"):
            product_dict["category"] = str(product_dict["category"]).strip().lower()
        if product_dict.get("slug"):
            product_dict["slug"] = str(product_dict["slug"]).strip().lower()
        product_dict["created_at"] = datetime.utcnow()
        result = await collection.insert_one(product_dict)
        product_dict["_id"] = result.inserted_id

        logger.info(f"Product created: {result.inserted_id}")
        return JewelryProduct(**product_dict)

    async def upsert_by_slug(self, product_data: JewelryProductCreate) -> JewelryProduct:
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]

        product_dict = product_data.model_dump()
        now = datetime.utcnow()
        await collection.update_one(
            {"slug": product_data.slug},
            {
                "$set": {**product_dict, "active": product_data.active},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        saved = await collection.find_one({"slug": product_data.slug})
        logger.info(f"Product upserted: {product_data.slug}")
        return JewelryProduct(**saved)
    
    async def get_by_id(self, product_id: str) -> Optional[JewelryProduct]:
        filt = await self._resolve_filter(product_id)
        if not filt:
            return await self.get_by_slug(product_id)

        collection = await self._collection()
        product = await collection.find_one(filt)
        if product:
            return JewelryProduct(**product)
        return None
    
    async def get_by_slug(self, slug: str) -> Optional[JewelryProduct]:
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]
        
        product = await collection.find_one({"slug": slug})
        if product:
            return JewelryProduct(**product)
        return None
    
    async def list(
        self,
        skip: int = 0,
        limit: int = 50,
        active: Optional[bool] = None,
        category: Optional[str] = None,
        is_best_seller: Optional[bool] = None,
        is_curated: Optional[bool] = None,
        sort: Optional[str] = None,
        search: Optional[str] = None,
        ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]
        
        query = self._list_query(
            active=active,
            category=category,
            is_best_seller=is_best_seller,
            is_curated=is_curated,
            search=search,
            ids=ids,
        )
        cursor = collection.find(query)
        
        if is_best_seller:
            cursor = cursor.sort([("best_seller_order", 1), ("created_at", -1)])
        elif is_curated:
            cursor = cursor.sort([("curated_order", 1), ("created_at", -1)])
        elif sort == "low":
            cursor = cursor.sort("price_inr", 1)
        elif sort == "high":
            cursor = cursor.sort("price_inr", -1)
        else:
            cursor = cursor.sort("created_at", -1)
        
        cursor = cursor.skip(skip).limit(limit)
        products = await cursor.to_list(length=limit)
        
        return [JewelryProduct(**product).model_dump(by_alias=True) for product in products]

    def _list_query(
        self,
        active: Optional[bool] = None,
        category: Optional[str] = None,
        is_best_seller: Optional[bool] = None,
        is_curated: Optional[bool] = None,
        search: Optional[str] = None,
        ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        query: Dict[str, Any] = {}
        if active is not None:
            query["active"] = active
        if category:
            query["category"] = category
        if is_best_seller is not None:
            query["is_best_seller"] = is_best_seller
        if is_curated is not None:
            query["is_curated"] = is_curated
        search_clause = None
        if search:
            safe = escape_mongo_regex(search)
            search_clause = {
                "$or": [
                    {"name": {"$regex": safe, "$options": "i"}},
                    {"description": {"$regex": safe, "$options": "i"}},
                    {"material": {"$regex": safe, "$options": "i"}},
                    {"slug": {"$regex": safe, "$options": "i"}},
                ]
            }
        return merge_mongo_filters(query or None, search_clause, ids_mongo_filter(ids))
    
    async def count(
        self,
        active: Optional[bool] = None,
        category: Optional[str] = None,
        is_best_seller: Optional[bool] = None,
        is_curated: Optional[bool] = None,
        search: Optional[str] = None,
        ids: Optional[List[str]] = None,
    ) -> int:
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]
        query = self._list_query(
            active=active,
            category=category,
            is_best_seller=is_best_seller,
            is_curated=is_curated,
            search=search,
            ids=ids,
        )
        return await collection.count_documents(query)
    
    async def update(
        self, product_id: str, update_data: Dict[str, Any], actor_email: Optional[str] = None
    ) -> Optional[JewelryProduct]:
        collection = await self._collection()
        product_filter = await self._resolve_filter(product_id)
        if not product_filter:
            return None

        protected = {"_id", "id", "created_at"}
        payload = {k: v for k, v in update_data.items() if k not in protected}
        if not payload:
            return await self.get_by_id(product_id)

        if "category" in payload and payload["category"]:
            payload["category"] = str(payload["category"]).strip().lower()
        if "slug" in payload and payload["slug"]:
            payload["slug"] = str(payload["slug"]).strip().lower()
            existing = await collection.find_one({
                "slug": payload["slug"],
                "_id": {"$ne": product_filter["_id"]},
            })
            if existing:
                raise ValueError(f"Product with slug '{payload['slug']}' already exists")

        # Always snapshot before the write so ANY change — price, stock, or
        # any other field (name/category/images/description/active/...) —
        # can be diffed and alerted on below, for both root's full edits and
        # a regional admin's price/stock-only edits. Previously this only
        # fetched a before-snapshot (and only ever alerted at all) when
        # `price_inr` itself was in the payload, which even for pure price
        # edits only happens when the edit includes the IN row — so most
        # edits, by anyone, produced no Telegram alert whatsoever.
        before = await collection.find_one(product_filter)

        result = await collection.update_one(product_filter, {"$set": payload})

        if result.matched_count == 0:
            return None
        saved = await collection.find_one(product_filter)
        updated = JewelryProduct(**saved) if saved else None

        if updated and before is not None and publish_alert:
            price_changes = (
                _diff_market_rows(
                    before.get("prices") or [], [p.model_dump() for p in updated.prices],
                    fields=("mrp", "sellingPrice"),
                )
                if "prices" in payload else []
            )
            stock_changes = (
                _diff_market_rows(
                    before.get("stock") or [], [s.model_dump() for s in updated.stock],
                    fields=("qty", "status"),
                )
                if "stock" in payload else []
            )
            field_changes = _diff_general_fields(before, payload)
            old_price, new_price = before.get("price_inr"), updated.price_inr
            price_inr_changed = "price_inr" in payload and old_price != new_price
            if price_changes or stock_changes or field_changes or price_inr_changed:
                await publish_alert(EVENT_PRODUCT_PRICE_CHANGED, {
                    "product_id": str(updated.id) if getattr(updated, "id", None) else product_id,
                    "product_name": updated.name,
                    "old_price": old_price,
                    "new_price": new_price,
                    "price_changes": price_changes,
                    "stock_changes": stock_changes,
                    "field_changes": field_changes,
                    "actor_email": actor_email,
                })

            # Same crossing rule a real purchase uses (src/services/
            # inventory_service.py's _atomic_decrement) — an admin manually
            # editing stock (whether qty, status, or both — an admin can
            # flip status alone, independent of qty, directly in the UI)
            # alerts identically, written once in evaluate_stock_crossing()
            # and reused here.
            for change in stock_changes:
                crossing = evaluate_stock_crossing(
                    change["old"].get("qty"),
                    change["new"].get("qty"),
                    settings.low_stock_threshold,
                    change["old"].get("status"),
                    change["new"].get("status"),
                )
                if not crossing:
                    continue
                event_type = event_type_for_stock_crossing(crossing)
                await publish_alert(event_type, {
                    "product_id": str(updated.id) if getattr(updated, "id", None) else product_id,
                    "product_name": updated.name,
                    "country": change["country"],
                    "qty": change["new"].get("qty"),
                    "threshold": settings.low_stock_threshold,
                    "actor_email": actor_email,
                })

        return updated
    
    async def delete(self, product_id: str) -> bool:
        product_filter = await self._resolve_filter(product_id)
        if not product_filter:
            return False

        collection = await self._collection()
        result = await collection.delete_one(product_filter)
        return result.deleted_count > 0
    
    async def get_by_slugs(self, slugs: List[str]) -> List[Dict[str, Any]]:
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]
        
        cursor = collection.find({"slug": {"$in": slugs}})
        products = await cursor.to_list(length=len(slugs))
        
        return [JewelryProduct(**product).model_dump(by_alias=True) for product in products]
