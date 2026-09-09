"""Regression tests for "every manual order created via the admin panel is
a ghost order": OrderService.create_from_admin() hardcoded
admin_order_country="IN", always priced from product.price_inr regardless
of the customer's actual region, hardcoded currency to INR, and never set
region_audit at all on the created order — the exact same defect being
backfilled for 53 historical orders, except it was happening on every NEW
manual order too. Uses a REAL Mongo connection (this project's established
pattern for service-level tests)."""
from __future__ import annotations

import uuid

import pytest

from src.database.connection import db
from src.models.product import JewelryProductCreate, MarketPrice, MarketStock
from src.services.inventory_service import InventoryService
from src.services.order_service import OrderService
from src.services.product_service import ProductService

PRODUCTS_COLLECTION = "products"
ORDERS_COLLECTION = "orders"


@pytest.fixture(autouse=True)
def _reset_mongo_singleton_per_test():
    db._client = None
    yield
    db._client = None


def _unique_slug() -> str:
    return f"order-region-test-{uuid.uuid4().hex[:10]}"


def _base_payload(product_id: str, country: str, quantity: int = 1) -> dict:
    return {
        "user_email": "customer@example.com",
        "country": country,
        "shipping_address": {
            "full_name": "Test Customer",
            "phone": "+61400000000",
            "address_line1": "1 Test St",
            "city": "Sydney",
            "state": "NSW",
            "postal_code": "2000",
            "country": "Australia",
        },
        "items": [{"product_id": product_id, "quantity": quantity}],
        "payment_method": "cod",
    }


@pytest.fixture
async def multi_region_product():
    service = ProductService()
    slug = _unique_slug()
    data = JewelryProductCreate(
        slug=slug, name="Region Pricing Test Ring", price_inr=1000,
        prices=[
            MarketPrice(country="IN", sym="₹", currency="INR", mrp=1000, sellingPrice=900),
            MarketPrice(country="AU", sym="$", currency="AUD", mrp=100, sellingPrice=90),
            MarketPrice(country="default", sym="$", currency="USD", mrp=50, sellingPrice=45),
        ],
        category="rings", collection="Chokmoki", thumbnail="thumb.jpg",
        gallery=["thumb.jpg"], sizes=[],
        stock=[
            MarketStock(country="IN", qty=50, status="in_stock"),
            MarketStock(country="AU", qty=20, status="in_stock"),
            MarketStock(country="default", qty=50, status="in_stock"),
        ],
    )
    product = await service.create(data)
    database = await db.get_database()
    try:
        yield product
    finally:
        await database[PRODUCTS_COLLECTION].delete_one({"_id": product.id})


@pytest.fixture
async def no_au_row_product():
    """No AU-specific price/stock row — must fall back to 'default'."""
    service = ProductService()
    slug = _unique_slug()
    data = JewelryProductCreate(
        slug=slug, name="India Only Test Ring", price_inr=1000,
        prices=[
            MarketPrice(country="IN", sym="₹", currency="INR", mrp=1000, sellingPrice=900),
            MarketPrice(country="default", sym="$", currency="USD", mrp=50, sellingPrice=45),
        ],
        category="rings", collection="Chokmoki", thumbnail="thumb.jpg",
        gallery=["thumb.jpg"], sizes=[],
    )
    product = await service.create(data)
    database = await db.get_database()
    try:
        yield product
    finally:
        await database[PRODUCTS_COLLECTION].delete_one({"_id": product.id})


@pytest.mark.asyncio
class TestAdminOrderRegionPricing:
    async def test_au_order_prices_from_au_market_row(self, multi_region_product):
        service = OrderService()
        database = await db.get_database()
        order = await service.create_from_admin(_base_payload(str(multi_region_product.id), "AU"))
        try:
            assert order.items[0].unit_price == 90
            assert order.currency == "AUD"
            assert order.currency_symbol == "$"
            assert order.region_audit is not None
            assert order.region_audit.pricing_country_used == "AU"
        finally:
            await database[ORDERS_COLLECTION].delete_one({"order_id": order.order_id})

    async def test_in_order_still_prices_from_in_market_row(self, multi_region_product):
        service = OrderService()
        database = await db.get_database()
        order = await service.create_from_admin(_base_payload(str(multi_region_product.id), "IN"))
        try:
            assert order.items[0].unit_price == 900
            assert order.currency == "INR"
            assert order.region_audit.pricing_country_used == "IN"
        finally:
            await database[ORDERS_COLLECTION].delete_one({"order_id": order.order_id})

    async def test_au_order_decrements_au_stock_bucket_not_india(self, multi_region_product):
        service = OrderService()
        database = await db.get_database()
        order = await service.create_from_admin(_base_payload(str(multi_region_product.id), "AU", quantity=3))
        try:
            fresh = await database[PRODUCTS_COLLECTION].find_one({"_id": multi_region_product.id})
            stock_by_country = {s["country"]: s["qty"] for s in fresh["stock"]}
            assert stock_by_country["AU"] == 17  # 20 - 3
            assert stock_by_country["IN"] == 50  # untouched
        finally:
            await database[ORDERS_COLLECTION].delete_one({"order_id": order.order_id})
            await InventoryService().release_committed_items(
                [order.items[0]], "AU"
            )

    async def test_no_country_specific_row_falls_back_to_default(self, no_au_row_product):
        service = OrderService()
        database = await db.get_database()
        order = await service.create_from_admin(_base_payload(str(no_au_row_product.id), "AU"))
        try:
            assert order.items[0].unit_price == 45
            assert order.currency == "USD"
            assert order.region_audit.pricing_country_used == "AU"
        finally:
            await database[ORDERS_COLLECTION].delete_one({"order_id": order.order_id})

    async def test_missing_country_in_payload_defaults_sanely_not_crash(self, multi_region_product):
        """Regression: an old-style payload omitting `country` (reachable
        from real production traffic during rollout) must still create an
        order, not throw."""
        service = OrderService()
        database = await db.get_database()
        payload = _base_payload(str(multi_region_product.id), country="")
        payload.pop("country")
        order = await service.create_from_admin(payload)
        try:
            assert order.region_audit is not None
            assert order.region_audit.pricing_country_used == "default"
        finally:
            await database[ORDERS_COLLECTION].delete_one({"order_id": order.order_id})
