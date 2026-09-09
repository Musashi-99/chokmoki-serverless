"""Regression tests for the "purchase-driven stock changes send no Telegram
alert at all" bug: InventoryService._atomic_decrement writes directly to
Mongo via $inc and never called ProductService.update() or publish_alert()
— so every real "just sold out" / "running low" moment was silent. Uses a
REAL Mongo connection (this project's established pattern for service-level
tests — see test_product_price_stock_alerts.py / test_user_null_identifier_regression.py),
with `publish_alert` patched so we can assert on what would have been sent."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.database.connection import db
from src.models.product import JewelryProductCreate, MarketPrice, MarketStock
from src.services.inventory_service import InventoryService
from src.services.product_service import ProductService

COLLECTION_NAME = "products"


@pytest.fixture(autouse=True)
def _reset_mongo_singleton_per_test():
    db._client = None
    yield
    db._client = None


def _unique_slug() -> str:
    return f"inv-alert-test-{uuid.uuid4().hex[:10]}"


async def _seed_product(qty: int, country: str = "AU") -> "JewelryProduct":  # noqa: F821
    service = ProductService()
    slug = _unique_slug()
    data = JewelryProductCreate(
        slug=slug, name="Inventory Alert Test Ring", price_inr=900,
        prices=[MarketPrice(country="default", sym="$", currency="USD", mrp=50, sellingPrice=45)],
        category="rings", collection="Chokmoki", thumbnail="thumb.jpg",
        gallery=["thumb.jpg"], sizes=[],
        stock=[
            MarketStock(country=country, qty=qty, status="in_stock"),
            MarketStock(country="default", qty=100, status="in_stock"),
        ],
    )
    return await service.create(data)


@pytest.fixture
async def product_15_units():
    product = await _seed_product(15)
    database = await db.get_database()
    try:
        yield product
    finally:
        await database[COLLECTION_NAME].delete_one({"_id": product.id})


@pytest.fixture
async def product_at_threshold_plus_one():
    product = await _seed_product(11)
    database = await db.get_database()
    try:
        yield product
    finally:
        await database[COLLECTION_NAME].delete_one({"_id": product.id})


@pytest.mark.asyncio
class TestPurchaseDrivenStockAlerts:
    async def test_decrement_staying_above_threshold_fires_no_alert(self, product_15_units):
        service = InventoryService()
        with patch("src.services.inventory_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            ok = await service._atomic_decrement(str(product_15_units.id), "AU", 2)
        assert ok is True
        mock_publish.assert_not_called()

    async def test_decrement_crossing_low_stock_threshold_fires_alert(self, product_at_threshold_plus_one):
        # 11 -> 9, threshold 10: crosses into low-stock.
        service = InventoryService()
        with patch("src.services.inventory_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            ok = await service._atomic_decrement(str(product_at_threshold_plus_one.id), "AU", 2)
        assert ok is True
        mock_publish.assert_called_once()
        event_type, payload = mock_publish.call_args[0]
        assert event_type == "product.low_stock"
        assert payload["qty"] == 9
        assert payload["threshold"] == 10
        assert payload["country"] == "AU"
        assert payload["product_name"] == "Inventory Alert Test Ring"

    async def test_decrement_emptying_stock_fires_out_of_stock_not_low_stock(self, product_15_units):
        service = InventoryService()
        with patch("src.services.inventory_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            ok = await service._atomic_decrement(str(product_15_units.id), "AU", 15)
        assert ok is True
        mock_publish.assert_called_once()
        event_type, payload = mock_publish.call_args[0]
        assert event_type == "product.out_of_stock"
        assert payload["qty"] == 0

    async def test_second_decrement_while_already_low_does_not_alert_again(self, product_at_threshold_plus_one):
        service = InventoryService()
        with patch("src.services.inventory_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service._atomic_decrement(str(product_at_threshold_plus_one.id), "AU", 2)  # 11 -> 9, alerts
            mock_publish.reset_mock()
            await service._atomic_decrement(str(product_at_threshold_plus_one.id), "AU", 2)  # 9 -> 7, no alert
        mock_publish.assert_not_called()

    async def test_insufficient_stock_decrement_fails_and_fires_no_alert(self, product_15_units):
        service = InventoryService()
        with patch("src.services.inventory_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            ok = await service._atomic_decrement(str(product_15_units.id), "AU", 999)
        assert ok is False
        mock_publish.assert_not_called()

    async def test_commit_items_end_to_end_fires_low_stock_alert(self, product_at_threshold_plus_one):
        """The real call path a checkout goes through — commit_items() ->
        _atomic_decrement() — not just the low-level method directly."""
        from src.models.order import ValidatedOrderItem

        service = InventoryService()
        item = ValidatedOrderItem(
            product_id=str(product_at_threshold_plus_one.id),
            product_name="Inventory Alert Test Ring",
            variant={"default": "default"},
            quantity=2,
            unit_price=100.0,
            total_price=200.0,
        )
        with patch("src.services.inventory_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service.commit_items([item], "AU")
        mock_publish.assert_called_once()
        event_type, _ = mock_publish.call_args[0]
        assert event_type == "product.low_stock"
