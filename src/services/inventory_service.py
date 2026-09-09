"""Product inventory reservations, atomic decrements, and reconciliation.

Stock is tracked per-region (src/models/product.py: MarketStock), not as a
single global number — a product can be sold out in India while still
available in Australia, and every operation here resolves against the
*specific* country an order is actually for (via src/pricing/stock_lookup.py
+ resolvers.py's same country-resolution chain pricing uses), never a
guess.

Important distinction: the *requested* region (e.g. "AU") is not always the
*bucket* actually holding the stock row (e.g. "default", if AU has no
dedicated row yet — see MarketStockEditor). Every Redis/Mongo key below is
keyed by the resolved *bucket*, not the raw requested region, or an AU
order would look for a nonexistent "AU" stock row and always fail as
"insufficient stock" even when the default bucket has plenty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from bson import ObjectId
from pymongo import ReturnDocument

from src.config import settings
from src.database.connection import db
from src.database.redis_connection import redis_client
from src.models.order import ValidatedOrderItem
from src.models.product import MarketStock
from src.pricing.stock_lookup import resolve_stock
from src.plugins.logger import logger
from src.services.stock_alerts import STOCK_EVENT_OUT_OF_STOCK, evaluate_stock_crossing

# Optional alerts import — same defensive pattern as product_service.py,
# so this file degrades gracefully (no alert, no crash) if src.alerts isn't
# importable in some context.
try:
    from src.alerts.events import (
        EVENT_PRODUCT_LOW_STOCK,
        EVENT_PRODUCT_OUT_OF_STOCK,
        publish_alert,
    )
except ImportError:
    EVENT_PRODUCT_LOW_STOCK = "product.low_stock"
    EVENT_PRODUCT_OUT_OF_STOCK = "product.out_of_stock"
    publish_alert = None


@dataclass(frozen=True)
class ReservationLine:
    product_id: str
    quantity: int
    # The stock bucket actually resolved against at reservation time (e.g.
    # "default" for an AU/NZ/ROW customer whose market has no dedicated
    # row) — persisted so commit/release later target the same bucket,
    # not whatever region string happens to be in scope (there may be
    # none) when those run.
    bucket: str


class InventoryService:
    COLLECTION_NAME = "products"
    # Durable mirror of the Redis reservation key — see reserve_for_order()'s
    # comment for why this exists. TTL sized generously past
    # inventory_reservation_ttl_seconds (1h default) so there's a real window
    # to recover a reservation whose Redis key already expired, without
    # keeping resolved rows around forever.
    RESERVATIONS_COLLECTION = "inventory_reservations"
    RESERVATIONS_TTL_SECONDS = 7 * 24 * 3600
    RESERVATION_PREFIX = "inv:order:"
    PENDING_QTY_PREFIX = "inv:pending:"
    LOCK_PREFIX = "inv:lock:"
    LOCK_TTL_SECONDS = 5

    @property
    def reservation_ttl(self) -> int:
        return settings.inventory_reservation_ttl_seconds

    async def ensure_indexes(self) -> None:
        database = await db.get_database()
        collection = database[self.RESERVATIONS_COLLECTION]
        await collection.create_index("order_id", unique=True)
        await collection.create_index([("status", 1), ("created_at", 1)])
        await collection.create_index(
            "created_at", expireAfterSeconds=self.RESERVATIONS_TTL_SECONDS, name="created_at_ttl"
        )

    def tracks_inventory(self, stock_qty: Optional[int]) -> bool:
        if not settings.inventory_enabled:
            return False
        return stock_qty is not None

    @staticmethod
    def _pending_key(product_id: str, bucket: str) -> str:
        return f"{InventoryService.PENDING_QTY_PREFIX}{product_id}:{bucket}"

    @staticmethod
    def _reservation_key(order_id: str) -> str:
        return f"{InventoryService.RESERVATION_PREFIX}{order_id}"

    @staticmethod
    def _lock_key(product_id: str, bucket: str) -> str:
        return f"{InventoryService.LOCK_PREFIX}{product_id}:{bucket}"

    async def _product_filter(self, collection, product_id: str) -> Optional[Dict[str, Any]]:
        if ObjectId.is_valid(product_id):
            return {"_id": ObjectId(product_id)}
        doc = await collection.find_one({"slug": product_id}, {"_id": 1})
        if not doc:
            return None
        return {"_id": doc["_id"]}

    async def _get_stock_entry(
        self, product_id: str, country: str
    ) -> Optional[MarketStock]:
        """The resolved region's stock row — `.country` on the result is
        the actual bucket holding it (e.g. "default"), which may differ
        from the requested `country` (e.g. "AU"). None if this product
        doesn't track inventory anywhere."""
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]
        filt = await self._product_filter(collection, product_id)
        if filt is None:
            return None
        doc = await collection.find_one(filt, {"stock": 1})
        if not doc:
            return None
        raw = doc.get("stock") or []
        if not raw:
            return None
        stock_list = [MarketStock(**s) for s in raw]
        return resolve_stock(stock_list, country)

    async def _acquire_product_lock(self, product_id: str, bucket: str) -> bool:
        redis = await redis_client.get_client()
        return bool(
            await redis.set(
                self._lock_key(product_id, bucket),
                "1",
                nx=True,
                ex=self.LOCK_TTL_SECONDS,
            )
        )

    async def _release_product_lock(self, product_id: str, bucket: str) -> None:
        redis = await redis_client.get_client()
        await redis.delete(self._lock_key(product_id, bucket))

    async def _get_pending_qty(self, product_id: str, bucket: str) -> int:
        redis = await redis_client.get_client()
        raw = await redis.get(self._pending_key(product_id, bucket))
        return int(raw or 0)

    async def _adjust_pending_qty(self, product_id: str, bucket: str, delta: int) -> None:
        redis = await redis_client.get_client()
        key = self._pending_key(product_id, bucket)
        if delta > 0:
            await redis.incrby(key, delta)
            return
        new_value = await redis.decrby(key, abs(delta))
        if new_value <= 0:
            await redis.delete(key)

    async def _atomic_decrement(self, product_id: str, bucket: str, quantity: int) -> bool:
        """`bucket` must be the resolved stock-row country (e.g. "default"),
        never a raw requested region — see module docstring."""
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]
        filt = await self._product_filter(collection, product_id)
        if filt is None:
            return False

        updated = await collection.find_one_and_update(
            {
                **filt,
                "stock": {"$elemMatch": {"country": bucket, "qty": {"$gte": quantity}}},
            },
            {"$inc": {"stock.$[elem].qty": -quantity}},
            array_filters=[{"elem.country": bucket}],
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            return False

        remaining = next(
            (s.get("qty") for s in (updated.get("stock") or []) if s.get("country") == bucket),
            None,
        )
        status = "out_of_stock" if (remaining is not None and remaining <= 0) else "in_stock"
        await collection.update_one(
            filt,
            {"$set": {"stock.$[elem].status": status}},
            array_filters=[{"elem.country": bucket}],
        )

        # Stock-level Telegram alert — the ONLY mutation this decrement
        # made was `-quantity`, so the pre-decrement qty is always exactly
        # `remaining + quantity`; no extra read needed.
        if remaining is not None and publish_alert:
            old_qty = remaining + quantity
            crossing = evaluate_stock_crossing(old_qty, remaining, settings.low_stock_threshold)
            if crossing:
                event_type = (
                    EVENT_PRODUCT_OUT_OF_STOCK
                    if crossing == STOCK_EVENT_OUT_OF_STOCK
                    else EVENT_PRODUCT_LOW_STOCK
                )
                await publish_alert(event_type, {
                    "product_id": str(updated.get("_id")) if updated.get("_id") else product_id,
                    "product_name": updated.get("name"),
                    "country": bucket,
                    "qty": remaining,
                    "threshold": settings.low_stock_threshold,
                })

        return True

    async def _sync_availability_status(self, product_id: str, bucket: str) -> None:
        """`bucket` must already be the resolved stock-row country."""
        try:
            entry = await self._get_stock_entry(product_id, bucket)
            if entry is None or entry.qty is None:
                return
            pending = await self._get_pending_qty(product_id, bucket)
            available = int(entry.qty) - pending
            database = await db.get_database()
            collection = database[self.COLLECTION_NAME]
            filt = await self._product_filter(collection, product_id)
            if filt is None:
                return
            status = "out_of_stock" if available <= 0 else "in_stock"
            await collection.update_one(
                filt,
                {"$set": {"stock.$[elem].status": status}},
                array_filters=[{"elem.country": bucket}],
            )
        except Exception as e:
            logger.warning(
                f"Inventory availability status sync failed for {product_id}/{bucket}: {e}"
            )

    async def _reserve_product(self, product_id: str, bucket: str, stock_qty: int, quantity: int) -> None:
        """`bucket`/`stock_qty` must come from an already-resolved
        MarketStock entry (see reserve_for_order)."""
        if not await self._acquire_product_lock(product_id, bucket):
            raise ValueError(f"Inventory lock unavailable for product {product_id}")

        try:
            pending = await self._get_pending_qty(product_id, bucket)
            available = int(stock_qty) - pending
            if quantity > available:
                raise ValueError(f"Insufficient stock for product {product_id}")

            await self._adjust_pending_qty(product_id, bucket, quantity)
            await self._sync_availability_status(product_id, bucket)
        finally:
            await self._release_product_lock(product_id, bucket)

    async def reserve_for_order(
        self, order_id: str, items: List[ValidatedOrderItem], country: str
    ) -> None:
        if not settings.inventory_enabled:
            return

        reserved: List[ReservationLine] = []
        redis = await redis_client.get_client()
        try:
            for item in items:
                entry = await self._get_stock_entry(item.product_id, country)
                if entry is None or not self.tracks_inventory(entry.qty):
                    continue
                await self._reserve_product(item.product_id, entry.country, entry.qty, item.quantity)
                reserved.append(
                    ReservationLine(
                        product_id=item.product_id, quantity=item.quantity, bucket=entry.country
                    )
                )

            if not reserved:
                return

            payload = json.dumps(
                [
                    {"product_id": line.product_id, "quantity": line.quantity, "bucket": line.bucket}
                    for line in reserved
                ]
            )
            await redis.setex(
                self._reservation_key(order_id),
                self.reservation_ttl,
                payload,
            )
            # Durable mirror, independent of the Redis key's TTL — if that
            # key expires before commit_reservation/release_reservation runs
            # (e.g. a payment recovered hours later via reconciliation), this
            # is what lets the pending-quantity hold still get released
            # instead of leaking forever. See commit_reservation/
            # release_reservation/reconcile_stale_reservations below.
            database = await db.get_database()
            await database[self.RESERVATIONS_COLLECTION].update_one(
                {"order_id": order_id},
                {
                    "$setOnInsert": {
                        "order_id": order_id,
                        "lines": [
                            {"product_id": line.product_id, "quantity": line.quantity, "bucket": line.bucket}
                            for line in reserved
                        ],
                        "status": "reserved",
                        "created_at": datetime.utcnow(),
                        "resolved_at": None,
                    }
                },
                upsert=True,
            )
        except Exception:
            for line in reserved:
                await self._adjust_pending_qty(line.product_id, line.bucket, -line.quantity)
            await redis.delete(self._reservation_key(order_id))
            raise

    async def _load_reservation(self, order_id: str) -> List[ReservationLine]:
        redis = await redis_client.get_client()
        raw = await redis.get(self._reservation_key(order_id))
        if not raw:
            return []
        data = json.loads(raw)
        return [
            ReservationLine(
                product_id=str(row["product_id"]),
                quantity=int(row["quantity"]),
                # Reservations written before per-region stock shipped (or
                # under the old "country" key name) have no "bucket" field
                # — "default" was the only bucket that ever existed then.
                bucket=str(row.get("bucket") or row.get("country") or "default"),
            )
            for row in data
        ]

    async def _resolve_durable_mirror(self, order_id: str, status: str) -> Optional[dict]:
        """Atomically mark the durable mirror resolved (committed/released)
        if it's still 'reserved' — a safe no-op if already resolved (by a
        concurrent caller or a prior run) or never written (inventory
        tracking disabled, or the order had no trackable lines). The atomic
        claim is what prevents double-processing if commit/release ever race
        on the same order.
        """
        database = await db.get_database()
        collection = database[self.RESERVATIONS_COLLECTION]
        return await collection.find_one_and_update(
            {"order_id": order_id, "status": "reserved"},
            {"$set": {"status": status, "resolved_at": datetime.utcnow()}},
            return_document=ReturnDocument.AFTER,
        )

    async def commit_reservation(self, order_id: str) -> None:
        lines = await self._load_reservation(order_id)
        from_mirror = None
        if not lines:
            from_mirror = await self._resolve_durable_mirror(order_id, "committed")
            if not from_mirror:
                return
            lines = [
                ReservationLine(
                    product_id=str(l["product_id"]),
                    quantity=int(l["quantity"]),
                    bucket=str(l.get("bucket") or l.get("country") or "default"),
                )
                for l in from_mirror["lines"]
            ]

        for line in lines:
            if not await self._atomic_decrement(line.product_id, line.bucket, line.quantity):
                logger.error(
                    f"Inventory commit failed for order {order_id} product {line.product_id}"
                )
                raise ValueError(f"Insufficient stock for product {line.product_id}")

        for line in lines:
            await self._adjust_pending_qty(line.product_id, line.bucket, -line.quantity)
        redis = await redis_client.get_client()
        await redis.delete(self._reservation_key(order_id))
        if from_mirror is None:
            # Redis path succeeded — still resolve the durable mirror so
            # reconcile_stale_reservations doesn't try to process it again.
            await self._resolve_durable_mirror(order_id, "committed")

    async def commit_items(self, items: List[ValidatedOrderItem], country: str) -> None:
        if not settings.inventory_enabled:
            return

        for item in items:
            entry = await self._get_stock_entry(item.product_id, country)
            if entry is None or not self.tracks_inventory(entry.qty):
                continue
            if not await self._atomic_decrement(item.product_id, entry.country, item.quantity):
                raise ValueError(f"Insufficient stock for product {item.product_id}")

    async def release_committed_items(
        self, items: List[ValidatedOrderItem], country: str
    ) -> None:
        """Reverse commit_items() when the following order write never landed."""
        if not settings.inventory_enabled:
            return
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]
        for item in items:
            entry = await self._get_stock_entry(item.product_id, country)
            if entry is None or not self.tracks_inventory(entry.qty):
                continue
            filt = await self._product_filter(collection, item.product_id)
            if filt is None:
                continue
            await collection.update_one(
                filt,
                {
                    "$inc": {"stock.$[elem].qty": item.quantity},
                    "$set": {"stock.$[elem].status": "in_stock"},
                },
                array_filters=[{"elem.country": entry.country}],
            )

    async def release_reservation(self, order_id: str) -> None:
        lines = await self._load_reservation(order_id)
        from_mirror = None
        if not lines:
            from_mirror = await self._resolve_durable_mirror(order_id, "released")
            if not from_mirror:
                return
            lines = [
                ReservationLine(
                    product_id=str(l["product_id"]),
                    quantity=int(l["quantity"]),
                    bucket=str(l.get("bucket") or l.get("country") or "default"),
                )
                for l in from_mirror["lines"]
            ]

        for line in lines:
            await self._adjust_pending_qty(line.product_id, line.bucket, -line.quantity)
            await self._sync_availability_status(line.product_id, line.bucket)
        redis = await redis_client.get_client()
        await redis.delete(self._reservation_key(order_id))
        if from_mirror is None:
            await self._resolve_durable_mirror(order_id, "released")

    async def reconcile_stale_reservations(self) -> int:
        redis = await redis_client.get_client()
        released = 0
        async for key in redis.scan_iter(match=f"{self.RESERVATION_PREFIX}*"):
            ttl = await redis.ttl(key)
            if ttl == -2:
                continue
            if ttl > 60:
                continue
            order_id = key.removeprefix(self.RESERVATION_PREFIX)
            await self.release_reservation(order_id)
            released += 1

        # Redis SCAN above only sees keys that still exist. Once a
        # reservation key's TTL (inventory_reservation_ttl_seconds) has
        # fully expired, it's invisible to that loop even though its
        # pending-quantity hold on stock was never released — this is
        # exactly the leak the durable mirror exists to close. Sweep it for
        # anything still "reserved" past when its Redis key should have
        # expired.
        database = await db.get_database()
        collection = database[self.RESERVATIONS_COLLECTION]
        cutoff = datetime.utcnow() - timedelta(seconds=self.reservation_ttl)
        async for doc in collection.find({"status": "reserved", "created_at": {"$lt": cutoff}}):
            await self.release_reservation(doc["order_id"])
            released += 1
        return released
