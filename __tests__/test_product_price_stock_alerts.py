"""Regression tests for the "Telegram alerts aren't dispatching for price
updates" bug: ProductService.update() only fetched a before-snapshot and
published EVENT_PRODUCT_PRICE_CHANGED when `price_inr` itself was in the
payload — which only happens when a `prices` edit includes the IN row.
A regional admin editing only AU/NZ prices, or a stock-only edit, produced
NO alert at all, and even when an alert did fire it carried no actor
attribution ("who edited what").

Uses a REAL Mongo connection (matches this project's established pattern
for service-level tests — see test_user_null_identifier_regression.py),
with `publish_alert` patched so we can assert on what would have been sent
without needing a real Redis stream/Telegram bot.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.database.connection import db
from src.models.product import JewelryProductCreate, MarketPrice, MarketStock
from src.services.product_service import ProductService, _diff_general_fields, _diff_market_rows

COLLECTION_NAME = "products"


@pytest.fixture(autouse=True)
def _reset_mongo_singleton_per_test():
    db._client = None
    yield
    # Reset on the way out too — otherwise this file's LAST test leaves a
    # Mongo client bound to its (function-scoped, now-closing) event loop
    # for whichever test file runs next in the same session, which then
    # hits "RuntimeError: Event loop is closed" the moment it touches Mongo
    # (same class of bug as scripts/smoke_test_region_scoping.py's
    # _run_async and the other Mongo-singleton test fixtures in this repo).
    db._client = None


def _unique_slug() -> str:
    return f"alert-test-{uuid.uuid4().hex[:10]}"


def _base_prices():
    return [
        MarketPrice(country="IN", sym="₹", currency="INR", mrp=1000, sellingPrice=900),
        MarketPrice(country="AU", sym="$", currency="AUD", mrp=100, sellingPrice=90),
        MarketPrice(country="default", sym="$", currency="USD", mrp=50, sellingPrice=45),
    ]


def _base_stock():
    return [
        MarketStock(country="IN", qty=10, status="in_stock"),
        MarketStock(country="AU", qty=5, status="in_stock"),
        MarketStock(country="default", qty=8, status="in_stock"),
    ]


@pytest.fixture
async def seeded_product():
    service = ProductService()
    slug = _unique_slug()
    data = JewelryProductCreate(
        slug=slug, name="Alert Test Ring", price_inr=900, prices=_base_prices(),
        category="rings", collection="Chokmoki", thumbnail="thumb.jpg",
        gallery=["thumb.jpg"], sizes=[], stock=_base_stock(),
    )
    product = await service.create(data)
    database = await db.get_database()
    try:
        yield product
    finally:
        await database[COLLECTION_NAME].delete_one({"_id": product.id})


class TestDiffMarketRows:
    def test_no_rows_changed_returns_empty(self):
        rows = [{"country": "AU", "mrp": 100, "sellingPrice": 90}]
        assert _diff_market_rows(rows, rows, fields=("mrp", "sellingPrice")) == []

    def test_changed_row_reports_old_and_new(self):
        before = [{"country": "AU", "mrp": 100, "sellingPrice": 90}]
        after = [{"country": "AU", "mrp": 100, "sellingPrice": 95}]
        changes = _diff_market_rows(before, after, fields=("mrp", "sellingPrice"))
        assert changes == [
            {"country": "AU", "old": {"mrp": 100, "sellingPrice": 90}, "new": {"mrp": 100, "sellingPrice": 95}}
        ]

    def test_unchanged_rows_alongside_changed_row_only_reports_the_changed_one(self):
        before = [
            {"country": "IN", "mrp": 1000, "sellingPrice": 900},
            {"country": "AU", "mrp": 100, "sellingPrice": 90},
        ]
        after = [
            {"country": "IN", "mrp": 1000, "sellingPrice": 900},
            {"country": "AU", "mrp": 100, "sellingPrice": 95},
        ]
        changes = _diff_market_rows(before, after, fields=("mrp", "sellingPrice"))
        assert [c["country"] for c in changes] == ["AU"]


class TestDiffGeneralFields:
    def test_no_change_returns_empty(self):
        before = {"name": "Ring", "active": True}
        assert _diff_general_fields(before, {"name": "Ring"}) == []

    def test_scalar_field_change_reports_old_and_new(self):
        before = {"name": "Old Name", "active": True}
        changes = _diff_general_fields(before, {"name": "New Name"})
        assert changes == [{"field": "name", "old": "Old Name", "new": "New Name"}]

    def test_active_toggle_reported(self):
        before = {"active": True}
        changes = _diff_general_fields(before, {"active": False})
        assert changes == [{"field": "active", "old": "True", "new": "False"}]

    def test_non_scalar_field_change_reported_without_dumping_content(self):
        before = {"gallery": ["a.jpg"]}
        changes = _diff_general_fields(before, {"gallery": ["a.jpg", "b.jpg"]})
        assert changes == [{"field": "gallery", "old": None, "new": None}]

    def test_prices_stock_price_inr_are_excluded_handled_separately(self):
        before = {"price_inr": 100, "prices": [], "stock": []}
        payload = {"price_inr": 200, "prices": [{"country": "IN"}], "stock": [{"country": "IN"}]}
        assert _diff_general_fields(before, payload) == []


@pytest.mark.asyncio
class TestProductServiceAlerts:
    async def test_root_editing_name_fires_alert_with_field_diff(self, seeded_product):
        service = ProductService()
        with patch("src.services.product_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service.update(
                str(seeded_product.id), {"name": "Renamed Ring"}, actor_email="root@chokmoki.com"
            )
        mock_publish.assert_called_once()
        _, payload = mock_publish.call_args[0]
        assert payload["actor_email"] == "root@chokmoki.com"
        assert payload["field_changes"] == [
            {"field": "name", "old": "Alert Test Ring", "new": "Renamed Ring"}
        ]
        assert payload["price_changes"] == []
        assert payload["stock_changes"] == []

    async def test_root_toggling_active_fires_alert(self, seeded_product):
        service = ProductService()
        with patch("src.services.product_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service.update(str(seeded_product.id), {"active": False}, actor_email="root@chokmoki.com")
        mock_publish.assert_called_once()
        _, payload = mock_publish.call_args[0]
        assert payload["field_changes"] == [{"field": "active", "old": "True", "new": "False"}]

    async def test_stock_only_edit_fires_alert_with_stock_diff(self, seeded_product):
        service = ProductService()
        new_stock = _base_stock()
        new_stock[1] = MarketStock(country="AU", qty=5, status="out_of_stock")
        payload = {"stock": [s.model_dump() for s in new_stock]}
        with patch("src.services.product_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service.update(str(seeded_product.id), payload, actor_email="regional@chokmoki.com")

        mock_publish.assert_called_once()
        event_type, payload = mock_publish.call_args[0]
        assert event_type == "product.price_changed"
        assert payload["actor_email"] == "regional@chokmoki.com"
        assert payload["price_changes"] == []
        assert len(payload["stock_changes"]) == 1
        assert payload["stock_changes"][0]["country"] == "AU"

    async def test_non_india_price_edit_fires_alert_with_price_diff(self, seeded_product):
        service = ProductService()
        new_prices = _base_prices()
        new_prices[1] = MarketPrice(country="AU", sym="$", currency="AUD", mrp=100, sellingPrice=99)
        payload = {"prices": [p.model_dump() for p in new_prices]}
        with patch("src.services.product_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service.update(str(seeded_product.id), payload, actor_email="regional@chokmoki.com")

        mock_publish.assert_called_once()
        _, payload = mock_publish.call_args[0]
        assert payload["actor_email"] == "regional@chokmoki.com"
        assert len(payload["price_changes"]) == 1
        assert payload["price_changes"][0]["country"] == "AU"
        # price_inr (IN row) untouched, so old/new price_inr are unchanged
        assert payload["old_price"] == payload["new_price"]

    async def test_no_actual_change_does_not_fire_alert(self, seeded_product):
        service = ProductService()
        unchanged = {"prices": [p.model_dump() for p in _base_prices()]}
        with patch("src.services.product_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            # Resend identical prices — nothing actually changed.
            await service.update(str(seeded_product.id), unchanged, actor_email="root@chokmoki.com")
        mock_publish.assert_not_called()

    async def test_edit_without_actor_email_still_fires_alert(self, seeded_product):
        """Legacy/CQRS callers that don't pass actor_email must not break —
        they just get no attribution in the alert (unchanged old behavior)."""
        service = ProductService()
        new_prices = _base_prices()
        new_prices[0] = MarketPrice(country="IN", sym="₹", currency="INR", mrp=1000, sellingPrice=850)
        payload = {"prices": [p.model_dump() for p in new_prices]}
        with patch("src.services.product_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service.update(str(seeded_product.id), payload)
        mock_publish.assert_called_once()
        _, sent_payload = mock_publish.call_args[0]
        assert sent_payload["actor_email"] is None
