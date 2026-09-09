from typing import Any, Dict, List, Optional
from bson import ObjectId
from datetime import datetime, timedelta
from pymongo import ReturnDocument
import asyncio
import uuid
import json
import razorpay.errors
from src.database.connection import db
from src.database.redis_connection import redis_client
from src.models.order import Order, OrderCreateInput, ValidatedOrderItem, OrderStatus, ShippingAddressInOrder, RegionAudit
from src.models.common import PricingDTO
from src.models.coupon import AppliedDiscount
from src.models.dto import OrderInitiateResponseDTO, OrderLogDTO
from src.models.product import Product
from src.services.product_service import ProductService
from src.services.discount_service import DiscountService
from src.services.inventory_service import InventoryService
from src.services.razorpay_service import RazorpayService
from src.services import order_ledger
from src.config import settings
from src.plugins.logger import logger
from src.plugins.structured_log import app_log
from src.fraud.models import FraudAction, FraudContext
from src.fraud.enrichment import FraudEnrichmentService
from src.services.fraud_detection_service import FraudDetectionService
from src.services.payment_reconciliation_service import PaymentReconciliationService
from src.resilience.circuit_breaker import CircuitBreaker
from src.resilience.guarded import call_guarded
from src.utils.regex_safe import escape_mongo_regex
from src.utils.money import money
from src.security.mongo_safe import coerce_safe_string
from src.services.user_service import UserService
from src.pricing.geo_provider import GeoIPDiscoveryAdapter
from src.pricing.resolvers import resolve_country
from src.pricing.price_lookup import resolve_price
from src.utils.region import is_india_address as _is_india_address

# Shared breaker state (lives in Redis — see circuit_breaker.py) for every
# outbound Razorpay call made from this service.
RAZORPAY_BREAKER = CircuitBreaker(
    "razorpay", failure_threshold=5, failure_window_seconds=60, cooldown_seconds=30
)

# Optional alerts import
try:
    from src.alerts.events import EVENT_ORDER_CREATED, publish_alert
except ImportError:
    EVENT_ORDER_CREATED = "order.created"
    publish_alert = None



def _order_alert_payload(order_dict: dict) -> dict:
    shipping_address = order_dict.get("shipping_address") or {}
    return {
        "order_id": order_dict.get("order_id"),
        "customer_name": shipping_address.get("full_name", ""),
        "customer_phone": shipping_address.get("phone", ""),
        "user_email": order_dict.get("user_email", ""),
        "total_amount": order_dict.get("total_amount", 0),
        "payment_method": order_dict.get("payment_method", ""),
        "items": [
            {"product_name": item.get("product_name", ""), "quantity": item.get("quantity", 1)}
            for item in order_dict.get("items", [])
        ],
    }


class OrderService:
    COLLECTION_NAME = "orders"
    ORDER_LOGS_COLLECTION = "order_logs"

    async def ensure_indexes(self) -> None:
        """Unique order_id prevents duplicate inserts under concurrent webhooks."""
        database = await db.get_database()
        orders_collection = database[self.COLLECTION_NAME]
        logs_collection = database[self.ORDER_LOGS_COLLECTION]
        await orders_collection.create_index("order_id", unique=True)
        # Non-unique — "my orders"/account lookups (list_for_customer,
        # list_by_phone) filter on this; without it those queries are a
        # full collection scan.
        await orders_collection.create_index("shipping_address.phone")
        # Non-unique — list_by_email/account "my orders" lookups filter on
        # this the same way list_by_phone does on shipping_address.phone.
        await orders_collection.create_index("user_email")
        # Sparse — leftover guest orders from before checkout started
        # attaching a captured user_id; still the strongest "my orders" match.
        await orders_collection.create_index("user_id", sparse=True)
        await logs_collection.create_index("order_id", unique=True)
        await order_ledger.ensure_indexes()

    # region -> currency actually used before `currency`/`currency_symbol`
    # were persisted on the order document (mirrors the storefront's
    # REGION_CURRENCY map at src/lib/regionCurrency.ts) — used only as a
    # best-effort fallback for orders written before that migration, never
    # for anything created going forward (those always carry the real,
    # resolved currency straight from the priced items).
    _LEGACY_CURRENCY_BY_COUNTRY = {
        "IN": ("INR", "₹"),
        "AU": ("AUD", "$"),
        "NZ": ("NZD", "$"),
        "default": ("USD", "$"),
    }

    def _order_from_doc(self, order_doc: dict) -> Order:
        """Single place every raw Mongo order document is turned into an
        `Order` — converts the embedded shipping address DTO and, for
        orders predating the `currency` field, infers it from the region
        that was actually used to price it rather than defaulting to INR
        (which previously made a real AU order's confirmation page and
        invoice show a rupee symbol on a non-Indian total)."""
        order_doc = {**order_doc}
        if isinstance(order_doc.get("shipping_address"), dict):
            order_doc["shipping_address"] = ShippingAddressInOrder(
                **order_doc["shipping_address"]
            )
        if not order_doc.get("currency"):
            region_audit = order_doc.get("region_audit") or {}
            country = region_audit.get("pricing_country_used") or "IN"
            currency, sym = self._LEGACY_CURRENCY_BY_COUNTRY.get(country, ("INR", "₹"))
            order_doc["currency"] = currency
            order_doc["currency_symbol"] = sym
        return Order(**order_doc)

    async def _clear_pending_redis(self, order_id: str) -> None:
        inventory_service = InventoryService()
        await inventory_service.release_reservation(order_id)
        redis = await redis_client.get_client()
        await redis.delete(f"pending_order:{order_id}")

    # A claim older than this is assumed abandoned (the claimer crashed
    # before finishing) and can be reclaimed by the next caller — otherwise a
    # crash mid-claim would leave the order stuck exactly like the original
    # bug this whole mechanism exists to fix, just one state later.
    POST_PROCESSING_STALE_CLAIM_SECONDS = 300

    async def _finish_post_processing(
        self,
        order_id: str,
        saved: dict,
        raw_order_log: dict,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        resolution_source: str,
    ) -> dict:
        """Runs (or re-runs) complete_pending_order()'s side effects — inventory
        commit, the one-shot order_logs snapshot, ledger entries, the Telegram
        alert, fraud-mark, reconcile-resolve — and only if they haven't
        already fully finished.

        Only re-runs when status is "pending" or a stale "in_progress" — not
        just "not done". Orders written before this field existed have it
        absent entirely (None), and were already fully processed by the code
        that existed at the time; treating "missing" as "needs repair" would
        replay ledger entries and alerts for every old, already-settled order
        the first time this new code touches it. Only new code ever writes
        "pending"/"in_progress", so only genuinely-interrupted runs match.

        The claim itself (`find_one_and_update` below) is atomic and is what
        makes it safe to gate the one-time actions (ledger append, alert) on
        post_processing_status at all: two callers racing on the same order
        (webhook + client-verify, or two reconcile passes) can't both pass
        the claim and both replay the ledger — only one `find_one_and_update`
        can match+flip "pending" -> "in_progress"; the loser sees no match
        and returns the current doc untouched. inventory commit / fraud-mark
        / reconcile-resolve / the log snapshot are each independently
        idempotent too, so even a stale-claim reclaim after a genuine crash
        can't double-apply their effects.
        """
        if saved.get("post_processing_status") not in ("pending", "in_progress"):
            return saved

        database = await db.get_database()
        orders_collection = database[self.COLLECTION_NAME]
        logs_collection = database[self.ORDER_LOGS_COLLECTION]

        now = datetime.utcnow()
        stale_cutoff = now - timedelta(seconds=self.POST_PROCESSING_STALE_CLAIM_SECONDS)
        claimed = await orders_collection.find_one_and_update(
            {
                "order_id": order_id,
                "$or": [
                    {"post_processing_status": "pending"},
                    {
                        "post_processing_status": "in_progress",
                        "post_processing_claimed_at": {"$lt": stale_cutoff},
                    },
                ],
            },
            {"$set": {"post_processing_status": "in_progress", "post_processing_claimed_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if claimed is None:
            # Another caller already claimed it (and hasn't gone stale) or
            # it finished in the moment between our read and this update —
            # don't duplicate their work, just report the current state.
            current = await orders_collection.find_one({"order_id": order_id})
            return current or saved

        inventory_service = InventoryService()
        await inventory_service.commit_reservation(order_id)
        log_dict = {
            "order_id": order_id,
            "raw_data": raw_order_log,
            "created_at": datetime.utcnow(),
        }
        await logs_collection.update_one(
            {"order_id": order_id}, {"$setOnInsert": log_dict}, upsert=True
        )
        if publish_alert:
            await publish_alert(EVENT_ORDER_CREATED, _order_alert_payload(claimed))
        await order_ledger.append_event(
            order_id, "order_created", "customer",
            {"payment_method": "razorpay", "total_amount": claimed.get("total_amount")},
        )
        await order_ledger.append_event(
            order_id, "payment_captured", "system",
            {"razorpay_order_id": razorpay_order_id, "razorpay_payment_id": razorpay_payment_id},
        )
        await FraudEnrichmentService.mark_order_completed(
            email=(claimed.get("user_email") or "").strip().lower(),
            payload={"items": claimed.get("items") or []},
        )
        await PaymentReconciliationService().mark_resolved(order_id=order_id, source=resolution_source)
        await orders_collection.update_one(
            {"order_id": order_id}, {"$set": {"post_processing_status": "done"}}
        )
        claimed["post_processing_status"] = "done"
        return claimed

    async def complete_pending_order(
        self,
        order_id: str,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        resolution_source: str = "webhook",
    ) -> tuple[Optional[Order], str]:
        """
        Atomically persist a paid order from Redis to MongoDB.
        Returns (order, status) where status is 'created', 'existing', or 'not_found'.
        """
        database = await db.get_database()
        orders_collection = database[self.COLLECTION_NAME]

        existing = await orders_collection.find_one({"order_id": order_id})
        if existing:
            # Found already — but if a previous call's post-processing (inventory
            # commit, ledger, alert, reconcile-resolve) crashed partway through,
            # returning immediately here would leave it stuck like that forever.
            # Finish it before returning, not just on the fresh-insert path below.
            existing = await self._finish_post_processing(
                order_id, existing, existing.get("raw_order_log", {}),
                razorpay_order_id, razorpay_payment_id, resolution_source,
            )
            await self._clear_pending_redis(order_id)
            return self._order_from_doc(existing), "existing"

        redis = await redis_client.get_client()
        redis_key = f"pending_order:{order_id}"
        order_json = await redis.get(redis_key)
        if not order_json:
            existing = await orders_collection.find_one({"order_id": order_id})
            if existing:
                existing = await self._finish_post_processing(
                    order_id, existing, existing.get("raw_order_log", {}),
                    razorpay_order_id, razorpay_payment_id, resolution_source,
                )
                return self._order_from_doc(existing), "existing"
            # Redis's pending_order key has a shorter TTL
            # (inventory_reservation_ttl_seconds, 1h default) than the payment
            # reconciliation window (2h default) — fall back to the durable
            # snapshot recorded at initiate_order() time so a late-arriving
            # webhook or reconciliation pass can still recover the order.
            attempt = await PaymentReconciliationService().get_attempt(order_id)
            snapshot = attempt.get("order_snapshot") if attempt else None
            if not snapshot:
                return None, "not_found"
            order_dict = dict(snapshot)
        else:
            order_dict = json.loads(order_json)
        expected_total = float(order_dict.get("total_amount") or 0)
        if razorpay_payment_id:
            razorpay_service = RazorpayService()
            # razorpay.Client is a blocking/synchronous HTTP client — offload
            # to a thread so this never stalls the event loop this coroutine
            # runs on (the worker's, when called from the webhook consumer;
            # the API's request-serving loop otherwise). Wrapped with
            # timeout+retry+breaker+bulkhead since this is a read-only GET —
            # safe to retry on transient failures, unlike create_order below.
            amount_ok = await call_guarded(
                dependency="razorpay",
                fn=lambda: asyncio.to_thread(
                    razorpay_service.verify_payment_amount, razorpay_payment_id, expected_total
                ),
                breaker=RAZORPAY_BREAKER,
                timeout_seconds=15.0,
                bulkhead_limit=10,
                retries=2,
                retryable_exceptions=(razorpay.errors.ServerError, razorpay.errors.GatewayError),
            )
            if not amount_ok:
                raise ValueError("Payment amount does not match order total")

        order_dict["payment_status"] = "completed"
        order_dict["razorpay_order_id"] = razorpay_order_id
        order_dict["razorpay_payment_id"] = razorpay_payment_id
        order_dict["created_at"] = datetime.fromisoformat(order_dict["created_at"])
        # Tracks whether _finish_post_processing's side effects actually
        # completed. Without this, a crash between the upsert succeeding and
        # those side effects finishing left the order permanently stuck: the
        # *next* call found it via the `existing` fast path above and used to
        # return immediately, never re-attempting inventory commit — a
        # silent, permanent oversell. Now both `existing` branches above also
        # check it.
        order_dict["post_processing_status"] = "pending"
        order_dict.pop("_id", None)

        result = await orders_collection.update_one(
            {"order_id": order_id},
            {"$setOnInsert": order_dict},
            upsert=True,
        )
        created = result.upserted_id is not None

        saved = await orders_collection.find_one({"order_id": order_id})
        if not saved:
            return None, "not_found"

        saved = await self._finish_post_processing(
            order_id, saved, order_dict.get("raw_order_log", {}),
            razorpay_order_id, razorpay_payment_id, resolution_source,
        )

        await self._clear_pending_redis(order_id)
        status = "created" if created else "existing"
        if status == "created":
            logger.info(f"Order {order_id} moved from Redis to MongoDB")
        else:
            logger.info(f"Order {order_id} already in MongoDB (concurrent completion)")
        return self._order_from_doc(saved), status
    
    async def reconcile_pending_payments(self, limit: int = 200) -> dict:
        """F-13: recover paid Razorpay orders stuck in Redis.

        If a webhook is lost or ``RAZORPAY_WEBHOOK_SECRET`` was unset, a captured
        payment never triggers ``complete_pending_order`` and the order expires
        from Redis after its TTL — the customer is charged but the order is lost.

        This job scans pending Razorpay orders, asks Razorpay for the
        authoritative payment state, and persists any order whose payment was
        actually captured/authorized. It is idempotent: ``complete_pending_order``
        upserts on the unique ``order_id`` index, so re-running (or racing the
        webhook) cannot create duplicates.

        Returns a summary dict: ``{checked, recovered, still_pending, errors}``.
        """
        redis = await redis_client.get_client()
        razorpay_service = RazorpayService()

        keys: list[str] = []
        async for key in redis.scan_iter(match="pending_order:*", count=100):
            keys.append(key)
            if len(keys) >= limit:
                break

        checked = 0
        recovered = 0
        still_pending = 0
        errors = 0

        for key in keys:
            raw = await redis.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue

            if data.get("payment_method") != "razorpay":
                continue
            order_id = data.get("order_id")
            razorpay_order_id = data.get("razorpay_order_id")
            if not order_id or not razorpay_order_id:
                # Older record written before F-13 stored the razorpay_order_id,
                # or order id missing — cannot reconcile without it.
                still_pending += 1
                continue

            checked += 1
            payment = await call_guarded(
                dependency="razorpay",
                fn=lambda rzp_id=razorpay_order_id: asyncio.to_thread(
                    razorpay_service.fetch_captured_payment, rzp_id
                ),
                breaker=RAZORPAY_BREAKER,
                timeout_seconds=15.0,
                bulkhead_limit=10,
                retries=2,
                retryable_exceptions=(razorpay.errors.ServerError, razorpay.errors.GatewayError),
            )
            if not payment:
                still_pending += 1
                continue

            try:
                _order, status = await self.complete_pending_order(
                    order_id, razorpay_order_id, payment["id"]
                )
                if status == "created":
                    recovered += 1
                    logger.info(
                        f"Reconciled paid order {order_id} "
                        f"(payment {payment['id']}) recovered from Razorpay"
                    )
            except Exception as e:
                errors += 1
                logger.error(f"Failed to reconcile order {order_id}: {e}")

        return {
            "checked": checked,
            "recovered": recovered,
            "still_pending": still_pending,
            "errors": errors,
        }

    def _validate_variant(self, product: Product, variant: dict) -> bool:
        """Validate that the variant matches the product's variant structure"""
        if not product.product_variants:
            # If product has no variants, only accept default variant
            return variant == {"default": "default"}
        
        # Check each variant key-value pair
        for variant_name, variant_value in variant.items():
            # Find matching variant definition in product
            product_variant = next(
                (pv for pv in product.product_variants if pv.variant_name == variant_name),
                None
            )
            
            if not product_variant:
                logger.warning(f"Variant name '{variant_name}' not found in product {product.id}")
                return False
            
            # Check if variant value exists and is active
            variant_value_obj = next(
                (vv for vv in product_variant.variant_values if vv.label == variant_value),
                None
            )
            
            if not variant_value_obj or not variant_value_obj.active:
                logger.warning(f"Variant value '{variant_value}' for '{variant_name}' not found or inactive in product {product.id}")
                return False
        
        # Check that all required variant names are provided
        variant_names_in_product = {pv.variant_name for pv in product.product_variants}
        variant_names_provided = set(variant.keys())
        
        if variant_names_in_product != variant_names_provided:
            logger.warning(f"Variant names mismatch. Product has: {variant_names_in_product}, provided: {variant_names_provided}")
            return False
        
        return True
    
    @staticmethod
    def _normalize_admin_payload(payload: dict) -> dict:
        """Accept both snake_case admin payloads and camelCase storefront shapes."""
        p = dict(payload)
        if "userEmail" in p and "user_email" not in p:
            p["user_email"] = p.pop("userEmail")
        if "shippingAddress" in p and "shipping_address" not in p:
            p["shipping_address"] = p.pop("shippingAddress")
        if "specialMessage" in p and "special_message" not in p:
            p["special_message"] = p.pop("specialMessage")
        if "paymentMethod" in p and "payment_method" not in p:
            p["payment_method"] = p.pop("paymentMethod")
        items = p.get("items") or []
        normalized_items = []
        for raw in items:
            item = dict(raw)
            if "productId" in item and "product_id" not in item:
                item["product_id"] = item.pop("productId")
            if "productName" in item and "product_name" not in item:
                item["product_name"] = item.pop("productName")
            if "price" in item and "unit_price" not in item:
                item["unit_price"] = item.pop("price")
            if "total" in item and "total_price" not in item:
                item["total_price"] = item.pop("total")
            normalized_items.append(item)
        p["items"] = normalized_items
        pricing = p.get("pricing")
        if isinstance(pricing, dict):
            p.setdefault("subtotal", pricing.get("subtotal", 0))
            p.setdefault("discount", pricing.get("discount", 0))
            p.setdefault("shipping", pricing.get("shipping", 0))
            p.setdefault("total_amount", pricing.get("total", 0))
        return p
    
    def _recalculate_pricing(self, validated_items: List[ValidatedOrderItem]) -> PricingDTO:
        """Recalculate pricing from validated items - never trust user data"""
        subtotal = money(sum(item.total_price for item in validated_items))
        return PricingDTO(
            subtotal=subtotal,
            discount=0,
            shipping=0,
            total=subtotal,
        )

    @staticmethod
    def _admin_order_totals(
        validated_items: List[ValidatedOrderItem],
        shipping: float = 0,
        discount: float = 0,
    ) -> tuple[float, float, float, float]:
        subtotal = money(sum(item.total_price for item in validated_items))
        shipping = money(max(0.0, float(shipping or 0)))
        discount = money(max(0.0, float(discount or 0)))
        total = money(max(0.0, subtotal + shipping - discount))
        return subtotal, shipping, discount, total
    
    async def create(
        self,
        order_data: OrderCreateInput,
        ip: Optional[str] = None,
        correlation_id: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Order:
        """Create order with validation and recalculation (for COD)"""
        if order_data.paymentMethod == "razorpay":
            raise ValueError("Use initiate_order for razorpay payments")

        validated_items, pricing, applied, region_audit = await self._validate_and_prepare_order(
            order_data, ip=ip, user_agent=user_agent
        )

        fraud_ctx = FraudContext(
            event_type="order_create",
            amount=float(pricing.total),
            email=order_data.userEmail,
            phone=order_data.shippingAddress.phone,
            endpoint="/api/orders",
            ip=ip,
            user_agent=user_agent,
            attributes={
                "selected_country": region_audit.selected_country,
                "ip_country": region_audit.ip_country,
                "country_mismatch": region_audit.country_mismatch,
            },
        )
        # Country mismatch is signal, never a blocker on its own — the fraud
        # rule for it (region_country_mismatch) is configured manual_review,
        # not reject. Only genuinely high-risk rules (velocity, tor, etc.)
        # can still REJECT here.
        decision = await FraudDetectionService().evaluate(ctx=fraud_ctx, payload=order_data.model_dump())
        if decision.action == FraudAction.REJECT:
            raise ValueError("Order rejected")

        inventory_service = InventoryService()
        await inventory_service.commit_items(validated_items, region_audit.pricing_country_used)
        committed_items = validated_items

        database = await db.get_database()
        orders_collection = database[self.COLLECTION_NAME]
        logs_collection = database[self.ORDER_LOGS_COLLECTION]

        raw_order_log = order_data.model_dump()
        order_id = str(uuid.uuid4())
        raw_order_log["order_id"] = order_id
        raw_order_log["received_at"] = datetime.utcnow().isoformat()
        raw_order_log["fraud_decision"] = decision.model_dump()

        user_id = await self._attach_checkout_account(order_data)

        # Convert to dict for MongoDB
        order_dict = {
            "order_id": order_id,
            "user_id": user_id,
            "user_email": order_data.userEmail,
            "shipping_address": order_data.shippingAddress.model_dump(),
            "items": [item.model_dump() for item in validated_items],
            "special_message": order_data.specialMessage or "",
            "subtotal": pricing.subtotal,
            "discount": pricing.discount,
            "shipping": pricing.shipping,
            "total_amount": pricing.total,
            # Every validated item was priced against the same resolved
            # country, so they all share one currency — take it from the
            # first item (guaranteed non-empty, see min-items validation).
            "currency": validated_items[0].currency,
            "currency_symbol": validated_items[0].sym,
            "payment_method": order_data.paymentMethod or "cod",
            # COD cash hasn't actually been collected yet at order time — admin
            # must explicitly mark it collected via mark_payment_collected().
            "payment_status": "pending",
            "status": OrderStatus(type="accepted").model_dump(),
            "created_at": datetime.utcnow(),
            "raw_order_log": raw_order_log,
            "region_audit": region_audit.model_dump(),
        }
        if applied is not None:
            order_dict["applied_discount"] = applied.model_dump()

        try:
            result = await orders_collection.insert_one(order_dict)
        except Exception:
            await inventory_service.release_committed_items(
                committed_items, region_audit.pricing_country_used
            )
            raise
        order_dict["_id"] = result.inserted_id

        # Convert log to dict for MongoDB
        log_dict = {
            "order_id": order_id,
            "raw_data": raw_order_log,
            "created_at": datetime.utcnow()
        }
        await logs_collection.insert_one(log_dict)

        if publish_alert:
            await publish_alert(EVENT_ORDER_CREATED, _order_alert_payload(order_dict))
        await order_ledger.append_event(
            order_id, "order_created", "customer",
            {"payment_method": order_dict["payment_method"], "total_amount": order_dict["total_amount"]},
        )
        # Only mark this exact cart (email+items) as "genuinely purchased"
        # now that it actually succeeded — see enrichment.py's
        # _duplicate_order_flag for why this must not happen on every attempt.
        await FraudEnrichmentService.mark_order_completed(
            email=order_data.userEmail.strip().lower(),
            payload={"items": [item.model_dump() for item in order_data.items]},
        )

        app_log(
            severity="INFO",
            module="order_service",
            event="order_created",
            correlation_id=correlation_id,
            order_id=order_id,
            total_amount=float(pricing.total),
            payment_method=order_dict["payment_method"],
            source="checkout",
        )
        return self._order_from_doc(order_dict)

    async def create_from_admin(
        self,
        payload: dict,
        ip: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Order:
        """Create an order from the admin dashboard (manual / phone orders)."""
        from src.models.region import normalize_region_code

        payload = self._normalize_admin_payload(payload)
        user_email = (payload.get("user_email") or "").strip()
        shipping_address = payload.get("shipping_address") or {}
        items_in = payload.get("items") or []
        # Which market this manual order is actually for — drives per-region
        # pricing, currency, and which stock bucket gets decremented (see
        # below). Defaults to "default" only if the caller genuinely omits
        # it; the admin UI always sends the admin's selected/assigned region.
        order_country = normalize_region_code(payload.get("country")) or "default"

        if not user_email:
            raise ValueError("user_email is required")
        if not shipping_address.get("full_name") or not shipping_address.get("phone"):
            raise ValueError("Customer name and phone are required")
        if not shipping_address.get("address_line1") or not shipping_address.get("city"):
            raise ValueError("Shipping address is incomplete")
        if not items_in:
            raise ValueError("At least one line item is required")

        product_service = ProductService()
        validated_items: List[ValidatedOrderItem] = []
        order_currency: Optional[str] = None
        order_currency_symbol: Optional[str] = None

        for raw in items_in:
            product_id = raw.get("product_id") or raw.get("productId")
            if not product_id:
                raise ValueError("Each line item needs a product_id")
            quantity = int(raw.get("quantity") or 0)
            if quantity < settings.order_min_quantity or quantity > settings.order_max_quantity:
                raise ValueError(
                    f"Quantity must be between {settings.order_min_quantity} "
                    f"and {settings.order_max_quantity}"
                )

            product = await product_service.get_by_id(str(product_id))
            if not product:
                raise ValueError(f"Product {product_id} not found")
            if not product.active:
                raise ValueError(f"Product {product.name} is not active")

            variant = raw.get("variant") or {}
            if not variant:
                variant = {"default": "default"}
            if not self._validate_variant(product, variant):
                raise ValueError(f"Invalid variant for product {product.name}")

            # Per-region price, not always India's — resolve_price is the
            # exact same Chain of Responsibility (exact country match, then
            # "default" bucket) real checkout uses (src/pricing/price_lookup.py),
            # reused here rather than re-deriving pricing logic.
            market_price = resolve_price(product.prices, order_country) if product.prices else None
            unit_price = money(market_price.sellingPrice) if market_price else money(product.price_inr)
            if market_price and order_currency is None:
                order_currency, order_currency_symbol = market_price.currency, market_price.sym
            validated_items.append(
                ValidatedOrderItem(
                    product_id=str(product.id),
                    product_name=raw.get("product_name") or product.name,
                    variant=variant,
                    quantity=quantity,
                    unit_price=unit_price,
                    total_price=money(unit_price * quantity),
                    size=raw.get("size"),
                )
            )

        subtotal, shipping, _ignored_discount, total_amount = self._admin_order_totals(
            validated_items,
            payload.get("shipping", 0),
            0,
        )
        applied = None
        code = (payload.get("coupon_code") or payload.get("couponCode") or "").strip()
        if code:
            pricing, applied = await DiscountService().apply(code, validated_items, shipping)
            subtotal = pricing.subtotal
            discount = pricing.discount
            shipping = pricing.shipping
            total_amount = pricing.total
        else:
            discount = 0.0
            total_amount = money(max(0.0, subtotal + shipping))

        payment_method_raw = (payload.get("payment_method") or "cod").strip().lower()
        payment_method = "razorpay" if payment_method_raw == "razorpay" else "cod"
        payment_status = payload.get("payment_status") or (
            "completed" if payment_method == "cod" else "pending"
        )

        status_payload = payload.get("status") or {}
        status = OrderStatus(
            type=status_payload.get("type", "accepted"),
            reason=status_payload.get("reason", "Created from dashboard"),
        )

        database = await db.get_database()
        orders_collection = database[self.COLLECTION_NAME]
        logs_collection = database[self.ORDER_LOGS_COLLECTION]

        order_id = str(uuid.uuid4())
        raw_order_log = {
            **payload,
            "order_id": order_id,
            "source": "admin_dashboard",
            "received_at": datetime.utcnow().isoformat(),
        }
        if settings.fraud_enabled:
            fraud_ctx = FraudContext(
                event_type="admin_order_create",
                amount=float(total_amount),
                email=user_email,
                phone=shipping_address.get("phone"),
                endpoint="/api/admin/orders",
                ip=ip,
            )
            decision = await FraudDetectionService().evaluate(ctx=fraud_ctx, payload=payload)
            raw_order_log["fraud_decision"] = decision.model_dump()
            if decision.action == FraudAction.REJECT:
                raise ValueError("Order rejected")

        shipping_address["email"] = shipping_address.get("email") or user_email

        order_dict = {
            "order_id": order_id,
            "user_email": user_email,
            "shipping_address": shipping_address,
            "items": [item.model_dump() for item in validated_items],
            "special_message": payload.get("special_message") or "",
            "subtotal": subtotal,
            "discount": discount,
            "shipping": shipping,
            "total_amount": total_amount,
            "currency": order_currency or "INR",
            "currency_symbol": order_currency_symbol or "₹",
            "payment_method": payment_method,
            "payment_status": payment_status,
            "status": status.model_dump(),
            "created_at": datetime.utcnow(),
            "raw_order_log": raw_order_log,
            # No GeoIP/session context for an admin-typed order — the rest
            # of the evidence-trail fields (ip_country, selected_country,
            # etc.) genuinely don't apply here the way they do for a real
            # checkout; pricing_country_used is what actually matters for
            # region filtering/reporting, and it's set to what the admin
            # explicitly picked, not guessed.
            "region_audit": RegionAudit(
                selected_country=order_country,
                pricing_country_used=order_country,
            ).model_dump(),
        }
        if applied is not None:
            order_dict["applied_discount"] = applied.model_dump()

        # Decrement the region the order was actually priced/created for —
        # previously always "IN" regardless of the real destination, which
        # silently decremented the wrong region's stock for any non-India
        # manual order.
        inventory_service = InventoryService()
        committed_items: List[ValidatedOrderItem] = []
        if payment_status == "completed":
            await inventory_service.commit_items(validated_items, order_country)
            committed_items = validated_items

        try:
            result = await orders_collection.insert_one(order_dict)
        except Exception:
            if committed_items:
                await inventory_service.release_committed_items(
                    committed_items, order_country
                )
            raise
        order_dict["_id"] = result.inserted_id

        log_dict = {
            "order_id": order_id,
            "raw_data": raw_order_log,
            "created_at": datetime.utcnow(),
        }
        await logs_collection.insert_one(log_dict)

        if publish_alert:
            await publish_alert(EVENT_ORDER_CREATED, _order_alert_payload(order_dict))
        await order_ledger.append_event(
            order_id, "order_created", "admin",
            {"payment_method": payment_method, "total_amount": total_amount, "source": "admin_dashboard"},
        )

        app_log(
            severity="INFO",
            module="order_service",
            event="order_created",
            correlation_id=correlation_id,
            order_id=order_id,
            total_amount=float(total_amount),
            payment_method=payment_method,
            source="admin_dashboard",
        )
        return self._order_from_doc(order_dict)

    async def get_by_id(self, order_id: str) -> Optional[Order]:
        """Get order by order_id (not MongoDB _id)"""
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]
        
        order_dict = await collection.find_one({"order_id": order_id})
        if order_dict:
            # Convert shipping_address dict to DTO
            return self._order_from_doc(order_dict)
        return None
    
    async def get_by_mongo_id(self, mongo_id: str) -> Optional[Order]:
        """Get order by MongoDB _id"""
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]
        
        order_dict = await collection.find_one({"_id": ObjectId(mongo_id)})
        if order_dict:
            # Convert shipping_address dict to DTO
            return self._order_from_doc(order_dict)
        return None
    
    async def list(
        self,
        skip: int = 0,
        limit: int = 20,
        user_email: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        coupon: Optional[str] = None,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
        sort_order: int = -1,
    ) -> List[Order]:
        """List orders with optional filtering, search, and date range.

        `countries`, when given, takes precedence over `country` and builds
        an $in filter — used by admin_orders.py to scope a region-restricted
        admin to their assigned set of regions (see
        api/routes/admin_orders.py's _region_filter)."""
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]

        query = self._build_order_query(
            user_email, status, search, from_date, to_date, coupon, country, countries
        )
        cursor = collection.find(query).sort("created_at", sort_order).skip(skip).limit(limit)
        orders_dict = await cursor.to_list(length=limit)

        orders = []
        for order_dict in orders_dict:
            orders.append(self._order_from_doc(order_dict))

        return orders

    async def list_for_customer(self, email: str, phone: str, limit: int = 50) -> List[Order]:
        """Look up a customer's own orders by email + phone (both required).

        Requiring both fields together — not email alone — keeps this public,
        unauthenticated endpoint from turning into an order-history leak for
        anyone who just knows a customer's email address.
        """
        email = coerce_safe_string(email, "email").strip().lower()
        phone = coerce_safe_string(phone, "phone").strip()
        if not email or not phone:
            return []

        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]
        query = {
            "user_email": {"$regex": f"^{escape_mongo_regex(email)}$", "$options": "i"},
            "shipping_address.phone": phone,
        }
        cursor = collection.find(query).sort("created_at", -1).limit(limit)
        orders_dict = await cursor.to_list(length=limit)

        orders = []
        for order_dict in orders_dict:
            orders.append(self._order_from_doc(order_dict))
        return orders

    async def list_by_phone(self, phone: str, limit: int = 50) -> List[Order]:
        """Authenticated variant of list_for_customer — the caller's phone
        is already proven by a verified customer session (src/services/
        customer_auth_service.py), so no email needs to be re-typed/matched.
        """
        phone = coerce_safe_string(phone, "phone").strip()
        if not phone:
            return []
        return await self._list_by_field("shipping_address.phone", phone, limit)

    async def list_by_email(self, email: str, limit: int = 50) -> List[Order]:
        """Same as list_by_phone but for an email-login account — matches
        Order.user_email, which every order (guest or logged-in) already
        stores, so a guest checkout under this email shows up here too the
        first time its owner logs in.
        """
        email = coerce_safe_string(email, "email").strip().lower()
        if not email:
            return []
        return await self._list_by_field("user_email", email, limit)

    async def list_by_user_id(self, user_id: str, limit: int = 50) -> List[Order]:
        """Strongest "my orders" match — the order was placed while this
        exact account's session was verified at checkout, not just guessed
        by matching phone/email text.
        """
        user_id = coerce_safe_string(user_id, "user_id").strip()
        if not user_id:
            return []
        return await self._list_by_field("user_id", user_id, limit)

    async def _list_by_field(self, field: str, value: str, limit: int) -> List[Order]:
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]
        cursor = (
            collection.find({field: value})
            .sort("created_at", -1)
            .limit(limit)
        )
        orders_dict = await cursor.to_list(length=limit)

        orders = []
        for order_dict in orders_dict:
            orders.append(self._order_from_doc(order_dict))
        return orders

    async def count(
        self,
        user_email: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        coupon: Optional[str] = None,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
    ) -> int:
        """Count orders matching the given filters. See list()'s docstring
        for `countries`."""
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]

        query = self._build_order_query(
            user_email, status, search, from_date, to_date, coupon, country, countries
        )
        return await collection.count_documents(query)

    @staticmethod
    def _parse_filter_datetime(value: str, *, end_of_day: bool = False) -> datetime:
        """Parse YYYY-MM-DD (admin date inputs) or ISO datetimes for list filters."""
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

    def _build_order_query(
        self,
        user_email: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        coupon: Optional[str] = None,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
    ) -> dict:
        query: dict = {}
        if user_email is not None:
            user_email = coerce_safe_string(user_email, "user_email")
        if status is not None:
            status = coerce_safe_string(status, "status")
        if search is not None:
            search = coerce_safe_string(search, "search")
        if from_date is not None:
            from_date = coerce_safe_string(from_date, "from_date")
        if to_date is not None:
            to_date = coerce_safe_string(to_date, "to_date")
        if coupon is not None:
            coupon = coerce_safe_string(coupon, "coupon")
        if country is not None:
            country = coerce_safe_string(country, "country")

        if user_email:
            query["user_email"] = user_email
        if status:
            query["status.type"] = status
        if coupon:
            query["applied_discount.code"] = coupon.upper()
        if countries:
            # Region-scoped admin restriction ($in over their assigned
            # regions) — takes precedence over the single `country` filter.
            normalized = [
                c.strip().upper() if c.strip().lower() != "default" else "default"
                for c in countries
                if c and c.strip()
            ]
            if normalized:
                query["region_audit.pricing_country_used"] = {"$in": normalized}
        elif country:
            # The region actually used to price the order — not the raw
            # selected/GeoIP fields, which can legitimately disagree with it
            # (that disagreement is its own signal, see country_mismatch).
            query["region_audit.pricing_country_used"] = country.strip().upper() if country.strip().lower() != "default" else "default"
        if search:
            safe = escape_mongo_regex(search)
            query["$or"] = [
                {"order_id": {"$regex": safe, "$options": "i"}},
                {"user_email": {"$regex": safe, "$options": "i"}},
                {"shipping_address.full_name": {"$regex": safe, "$options": "i"}},
                {"shipping_address.phone": {"$regex": safe, "$options": "i"}},
                {"applied_discount.code": {"$regex": safe, "$options": "i"}},
            ]
        if from_date or to_date:
            date_filter: dict = {}
            if from_date:
                date_filter["$gte"] = self._parse_filter_datetime(from_date, end_of_day=False)
            if to_date:
                date_filter["$lte"] = self._parse_filter_datetime(to_date, end_of_day=True)
            if date_filter:
                query["created_at"] = date_filter
        return query
    
    async def get_order_log(self, order_id: str) -> Optional[OrderLogDTO]:
        """Get raw order log for debugging"""
        database = await db.get_database()
        logs_collection = database[self.ORDER_LOGS_COLLECTION]
        
        log_dict = await logs_collection.find_one({"order_id": order_id})
        if log_dict:
            # Convert datetime to string if needed
            if isinstance(log_dict.get("created_at"), datetime):
                log_dict["created_at"] = log_dict["created_at"].isoformat()
            return OrderLogDTO(**log_dict)
        return None
    
    async def update_status(
        self,
        order_id: str,
        status: OrderStatus,
        actor: str = "admin",
    ) -> Optional[Order]:
        """Update order status by order_id"""
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]

        existing = await collection.find_one({"order_id": order_id}, {"status": 1})
        previous_type = (existing or {}).get("status", {}).get("type")

        result = await collection.update_one(
            {"order_id": order_id},
            {"$set": {"status": status.model_dump()}}
        )

        if result.matched_count == 0:
            return None
        await order_ledger.append_event(
            order_id, "status_changed", actor,
            {"from": previous_type, "to": status.type, "reason": status.reason},
        )
        return await self.get_by_id(order_id)

    async def mark_payment_collected(self, order_id: str, actor: str) -> Optional[Order]:
        """Admin-explicit action: cash for a COD order has actually been
        collected. Replaces the old implicit "COD = paid at order time"
        assumption — payment_status now starts 'pending' for every order.
        """
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]

        result = await collection.update_one(
            {"order_id": order_id},
            {"$set": {"payment_status": "completed"}},
        )
        if result.matched_count == 0:
            return None
        await order_ledger.append_event(order_id, "payment_marked_collected", actor, {})
        return await self.get_by_id(order_id)

    async def set_custom_status(self, order_id: str, custom_status: Optional[str], actor: str) -> Optional[Order]:
        """Admin-only operational tag, orthogonal to status.type — never
        touched by webhooks or system logic.
        """
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]

        existing = await collection.find_one({"order_id": order_id}, {"custom_status": 1})
        if existing is None:
            return None
        previous = existing.get("custom_status")

        await collection.update_one(
            {"order_id": order_id},
            {"$set": {"custom_status": custom_status}},
        )
        await order_ledger.append_event(
            order_id, "custom_status_changed", actor, {"from": previous, "to": custom_status},
        )
        return await self.get_by_id(order_id)

    async def add_note(self, order_id: str, text: str, author_email: str) -> Optional[Order]:
        """Append-only admin annotation — no edit/delete, this is a record."""
        database = await db.get_database()
        collection = database[self.COLLECTION_NAME]

        note = {"text": text, "author_email": author_email, "created_at": datetime.utcnow().isoformat()}
        result = await collection.update_one(
            {"order_id": order_id},
            {"$push": {"notes": note}},
        )
        if result.matched_count == 0:
            return None
        await order_ledger.append_event(order_id, "note_added", author_email, {"text": text})
        return await self.get_by_id(order_id)

    async def get_events(self, order_id: str) -> List[Dict[str, Any]]:
        return await order_ledger.get_events(order_id)


    async def _resolve_region(
        self, order_data: OrderCreateInput, ip: Optional[str], user_agent: Optional[str]
    ) -> RegionAudit:
        """Single place that computes the GeoIP lookup + effective pricing
        country + full evidence trail — called once per order so the same
        lookup feeds both pricing and the fraud enrichment step below."""
        geo = await GeoIPDiscoveryAdapter().lookup(ip or "")
        selected = (order_data.selectedCountry or "").strip().upper() or None
        effective_country = resolve_country(selected_country=selected, ip_country=geo.country)
        return RegionAudit(
            ip=ip,
            ip_country=geo.country,
            ip_country_raw=geo.raw_country,
            selected_country=selected,
            pricing_country_used=effective_country,
            country_mismatch=bool(selected and geo.country and selected != geo.country and geo.country != "default"),
            user_agent=user_agent,
            geoip_raw_response=geo.raw,
        )

    async def _validate_and_prepare_order(
        self,
        order_data: OrderCreateInput,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> tuple[List[ValidatedOrderItem], PricingDTO, Optional[AppliedDiscount], RegionAudit]:
        """Validate order items and recalculate pricing - shared logic"""
        product_service = ProductService()
        validated_items: List[ValidatedOrderItem] = []

        region_audit = await self._resolve_region(order_data, ip, user_agent)
        country = region_audit.pricing_country_used

        # COD needs no payment gateway, so it's available from every market.
        # Paying online now (Razorpay) only works in INR today — reject that
        # combination early, before inventory/fraud work, with a message the
        # storefront can show as-is. Shipping/courier fulfillment is a
        # separate, later gate (see InventoryService/CourierProvider) — this
        # is only the payment-capability check.
        payment_method = (order_data.paymentMethod or "cod").strip().lower()
        if payment_method == "razorpay":
            prepaid_enabled = {
                c.strip().upper() for c in (settings.prepaid_enabled_countries or "").split(",") if c.strip()
            }
            if country not in prepaid_enabled:
                raise ValueError(
                    "Online prepaid payment isn't available in your region yet — "
                    "please choose Cash on Delivery, or switch to India to pay online."
                )
            # Automated fulfillment (Shiprocket) only ships within India today
            # (see src/shipping/courier_provider.py), and an online-paid order
            # has no manual-collection fallback the way COD does, so a
            # non-Indian shipping address paired with prepaid checkout is
            # rejected here.
            if not _is_india_address(order_data.shippingAddress.country):
                raise ValueError(
                    "We can only ship within India right now — please use an Indian shipping address."
                )
        # COD orders outside India are allowed through deliberately: Shiprocket
        # fulfillment stays India-only (CourierProvider resolves to
        # UnavailableCourierProvider for any other market, see
        # ShiprocketPanel's manual-handling banner), but the order itself
        # isn't blocked — admins fulfil/deliver those manually.

        min_qty = settings.order_min_quantity
        max_qty = settings.order_max_quantity

        for item in order_data.items:
            if item.quantity < min_qty or item.quantity > max_qty:
                raise ValueError(
                    f"Quantity must be between {min_qty} and {max_qty} for product {item.productId}"
                )

            product = await product_service.get_by_id(item.productId)
            if not product:
                raise ValueError(f"Product {item.productId} not found")
            if not product.active:
                raise ValueError(f"Product {item.productId} is not active")
            variant = item.variant or {}
            if not variant:
                variant = {"default": "default"}
            if not self._validate_variant(product, variant):
                raise ValueError(f"Invalid variant {item.variant} for product {item.productId}")

            market_price = resolve_price(product.prices, country) if product.prices else None
            unit_price = money(market_price.sellingPrice) if market_price else money(product.price_inr)
            total_price = money(unit_price * item.quantity)
            # Legacy products with no `prices` array at all are still priced
            # off `price_inr` directly (always INR) — everything else carries
            # whichever currency its resolved MarketPrice bucket declares,
            # "default" included (see scripts/migrate_market_prices.py).
            item_currency = market_price.currency if market_price else "INR"
            item_sym = market_price.sym if market_price else "₹"

            validated_items.append(ValidatedOrderItem(
                product_id=str(product.id),
                product_name=product.name,
                variant=variant,
                currency=item_currency,
                sym=item_sym,
                quantity=item.quantity,
                unit_price=unit_price,
                total_price=total_price,
                size=item.size
            ))

        pricing = self._recalculate_pricing(validated_items)
        applied = None
        code = (order_data.couponCode or "").strip()
        if code:
            pricing, applied = await DiscountService().apply(code, validated_items, pricing.shipping, country=country)
        return validated_items, pricing, applied, region_audit

    async def _attach_checkout_account(self, order_data: OrderCreateInput) -> Optional[str]:
        addr = order_data.shippingAddress
        try:
            captured = await UserService().capture_from_checkout(
                full_name=addr.full_name,
                phone=addr.phone,
                email=order_data.userEmail or addr.email or "",
                address_line1=addr.address_line1,
                address_line2=addr.address_line2 or "",
                city=addr.city,
                state=addr.state or "",
                postal_code=addr.postal_code,
                country=addr.country,
                existing_user_id=order_data.user_id,
            )
        except Exception as e:
            if logger:
                logger.warning(f"Checkout account capture failed: {e}")
            return order_data.user_id
        return captured or order_data.user_id
    
    async def initiate_order(
        self, order_data: OrderCreateInput, ip: Optional[str] = None, user_agent: Optional[str] = None
    ) -> OrderInitiateResponseDTO:
        """Initiate order: validate, store in Redis, create Razorpay order"""
        if order_data.paymentMethod != "razorpay":
            raise ValueError("initiate_order only supports razorpay payment method")

        validated_items, pricing, applied, region_audit = await self._validate_and_prepare_order(
            order_data, ip=ip, user_agent=user_agent
        )

        fraud_ctx = FraudContext(
            event_type="order_initiate",
            amount=float(pricing.total),
            email=order_data.userEmail,
            phone=order_data.shippingAddress.phone,
            endpoint="/api/orders/initiate",
            ip=ip,
            user_agent=user_agent,
            attributes={
                "selected_country": region_audit.selected_country,
                "ip_country": region_audit.ip_country,
                "country_mismatch": region_audit.country_mismatch,
            },
        )
        decision = await FraudDetectionService().evaluate(ctx=fraud_ctx, payload=order_data.model_dump())
        if decision.action == FraudAction.REJECT:
            raise ValueError("Order rejected")
        
        order_id = str(uuid.uuid4())
        inventory_service = InventoryService()
        await inventory_service.reserve_for_order(
            order_id, validated_items, region_audit.pricing_country_used
        )

        raw_order_log = order_data.model_dump()
        raw_order_log["order_id"] = order_id
        raw_order_log["received_at"] = datetime.utcnow().isoformat()
        raw_order_log["fraud_decision"] = decision.model_dump()

        user_id = await self._attach_checkout_account(order_data)

        # Convert to dict for Redis storage
        order_dict = {
            "order_id": order_id,
            "user_id": user_id,
            "user_email": order_data.userEmail,
            "shipping_address": order_data.shippingAddress.model_dump(),
            "items": [item.model_dump() for item in validated_items],
            "special_message": order_data.specialMessage or "",
            "subtotal": pricing.subtotal,
            "discount": pricing.discount,
            "shipping": pricing.shipping,
            "total_amount": pricing.total,
            "currency": validated_items[0].currency,
            "currency_symbol": validated_items[0].sym,
            "payment_method": "razorpay",
            "payment_status": "pending",
            "status": OrderStatus(type="accepted").model_dump(),
            "created_at": datetime.utcnow().isoformat(),
            "raw_order_log": raw_order_log,
            "region_audit": region_audit.model_dump(mode="json"),
        }
        if applied is not None:
            order_dict["applied_discount"] = applied.model_dump()

        redis = await redis_client.get_client()
        redis_key = f"pending_order:{order_id}"
        await redis.setex(
            redis_key,
            settings.inventory_reservation_ttl_seconds,
            json.dumps(order_dict, default=str),
        )
        
        razorpay_service = RazorpayService()
        # Blocking/synchronous HTTP call — offload to a thread so a slow
        # Razorpay API response can't stall the API process's event loop,
        # which is shared by every other in-flight request. Deliberately
        # retries=0: create_order is NOT idempotent on Razorpay's side (no
        # idempotency key passed), so blindly retrying a slow-but-actually-
        # successful call risks creating two real Razorpay orders for one
        # checkout. Timeout+breaker+bulkhead still apply — a hung/failing
        # Razorpay fails fast instead of hanging the request or piling up
        # threads, it just doesn't get retried here.
        razorpay_order = await call_guarded(
            dependency="razorpay",
            fn=lambda: asyncio.to_thread(
                razorpay_service.create_order,
                amount=pricing.total,
                notes={"order_id": order_id, "user_email": order_data.userEmail},
            ),
            breaker=RAZORPAY_BREAKER,
            timeout_seconds=15.0,
            bulkhead_limit=10,
            retries=0,
        )

        # F-13: persist the Razorpay order id into the pending record so the
        # reconciliation job can query Razorpay for captured payments whose
        # webhook never arrived. `keepttl` preserves the reservation TTL.
        order_dict["razorpay_order_id"] = razorpay_order.id
        await redis.set(redis_key, json.dumps(order_dict, default=str), keepttl=True)

        logger.info(f"Order initiated: {order_id}, Razorpay order: {razorpay_order.id}")
        await order_ledger.append_event(
            order_id, "payment_initiated", "customer",
            {"razorpay_order_id": razorpay_order.id, "amount": pricing.total},
        )
        # Durable, time-queryable record independent of the Redis
        # pending_order key's TTL — see PaymentReconciliationService.
        await PaymentReconciliationService().record_attempt(
            order_id=order_id,
            razorpay_order_id=razorpay_order.id,
            user_email=order_data.userEmail,
            total_amount=pricing.total,
            order_snapshot=json.loads(json.dumps(order_dict, default=str)),
        )

        return OrderInitiateResponseDTO(
            order_id=order_id,
            razorpay_order_id=razorpay_order.id,
            razorpay_key_id=settings.razorpay_key_id,
            amount=pricing.total
        )
    
    async def verify_payment(
        self,
        order_id: str,
        razorpay_order_id: str,
        razorpay_payment_id: str,
        razorpay_signature: str
    ) -> Order:
        """Verify payment signature and move order from Redis to MongoDB"""
        razorpay_service = RazorpayService()
        
        if not razorpay_service.verify_payment_signature(
            razorpay_order_id, razorpay_payment_id, razorpay_signature
        ):
            raise ValueError("Invalid payment signature")

        order, status = await self.complete_pending_order(
            order_id, razorpay_order_id, razorpay_payment_id, resolution_source="client_verify"
        )
        if status == "not_found":
            raise ValueError(f"Order {order_id} not found in Redis")
        return order
