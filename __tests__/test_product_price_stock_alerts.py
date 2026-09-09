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


@pytest.fixture
async def seeded_product_high_stock():
    """AU starts well above the low-stock threshold (10), unlike
    seeded_product's AU=5 (already-low) — needed to actually exercise a
    crossing rather than a no-op."""
    service = ProductService()
    slug = _unique_slug()
    data = JewelryProductCreate(
        slug=slug, name="Alert Test Ring High Stock", price_inr=900, prices=_base_prices(),
        category="rings", collection="Chokmoki", thumbnail="thumb.jpg",
        gallery=["thumb.jpg"], sizes=[],
        stock=[
            MarketStock(country="IN", qty=50, status="in_stock"),
            MarketStock(country="AU", qty=15, status="in_stock"),
            MarketStock(country="default", qty=50, status="in_stock"),
        ],
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

        # Two alerts now: the general "Product Updated" summary, AND a
        # dedicated out-of-stock alert (status-driven crossing — qty stayed
        # 5, only status flipped in_stock -> out_of_stock).
        event_types = [call.args[0] for call in mock_publish.call_args_list]
        assert "product.price_changed" in event_types
        assert "product.out_of_stock" in event_types

        summary_call = next(c for c in mock_publish.call_args_list if c.args[0] == "product.price_changed")
        payload = summary_call.args[1]
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


@pytest.mark.asyncio
class TestAdminStockCrossingAlerts:
    """Same crossing rule a real purchase uses (evaluate_stock_crossing),
    reused for an admin's manual stock edit."""

    async def test_admin_edit_crossing_low_stock_threshold_fires_alert(self, seeded_product_high_stock):
        service = ProductService()
        new_stock = [
            {"country": "IN", "qty": 50, "status": "in_stock"},
            {"country": "AU", "qty": 8, "status": "in_stock"},  # 15 -> 8, crosses threshold 10
            {"country": "default", "qty": 50, "status": "in_stock"},
        ]
        with patch("src.services.product_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service.update(
                str(seeded_product_high_stock.id), {"stock": new_stock}, actor_email="regional@chokmoki.com"
            )
        event_types = [call.args[0] for call in mock_publish.call_args_list]
        assert "product.low_stock" in event_types
        low_stock_call = next(c for c in mock_publish.call_args_list if c.args[0] == "product.low_stock")
        payload = low_stock_call.args[1]
        assert payload["qty"] == 8
        assert payload["country"] == "AU"
        assert payload["actor_email"] == "regional@chokmoki.com"

    async def test_admin_edit_crossing_to_zero_fires_out_of_stock(self, seeded_product_high_stock):
        service = ProductService()
        new_stock = [
            {"country": "IN", "qty": 50, "status": "in_stock"},
            {"country": "AU", "qty": 0, "status": "out_of_stock"},
            {"country": "default", "qty": 50, "status": "in_stock"},
        ]
        with patch("src.services.product_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service.update(
                str(seeded_product_high_stock.id), {"stock": new_stock}, actor_email="root@chokmoki.com"
            )
        event_types = [call.args[0] for call in mock_publish.call_args_list]
        assert "product.out_of_stock" in event_types
        assert "product.low_stock" not in event_types

    async def test_admin_restocking_above_threshold_fires_no_stock_level_alert(self, seeded_product_high_stock):
        service = ProductService()
        new_stock = [
            {"country": "IN", "qty": 50, "status": "in_stock"},
            {"country": "AU", "qty": 30, "status": "in_stock"},  # 15 -> 30, a restock
            {"country": "default", "qty": 50, "status": "in_stock"},
        ]
        with patch("src.services.product_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service.update(
                str(seeded_product_high_stock.id), {"stock": new_stock}, actor_email="root@chokmoki.com"
            )
        event_types = [call.args[0] for call in mock_publish.call_args_list]
        assert "product.low_stock" not in event_types
        assert "product.out_of_stock" not in event_types


class TestFormatPriceChangedAlertShowsBothMrpAndSelling:
    def test_mrp_only_change_renders_mrp_distinctly(self):
        from src.alerts.handlers import _format_price_changed_alert

        payload = {
            "product_name": "Test Ring",
            "price_changes": [
                {"country": "AU", "old": {"mrp": 100, "sellingPrice": 90}, "new": {"mrp": 120, "sellingPrice": 90}}
            ],
        }
        text = _format_price_changed_alert(payload)
        assert "MRP" in text
        assert "₹100" in text and "₹120" in text
        # Selling price didn't change — must not claim it did.
        assert "Selling" not in text

    def test_selling_price_only_change_renders_selling_distinctly(self):
        from src.alerts.handlers import _format_price_changed_alert

        payload = {
            "product_name": "Test Ring",
            "price_changes": [
                {"country": "AU", "old": {"mrp": 100, "sellingPrice": 90}, "new": {"mrp": 100, "sellingPrice": 95}}
            ],
        }
        text = _format_price_changed_alert(payload)
        assert "Selling" in text
        assert "₹90" in text and "₹95" in text
        assert "MRP" not in text

    def test_both_mrp_and_selling_change_render_both(self):
        from src.alerts.handlers import _format_price_changed_alert

        payload = {
            "product_name": "Test Ring",
            "price_changes": [
                {"country": "AU", "old": {"mrp": 100, "sellingPrice": 90}, "new": {"mrp": 110, "sellingPrice": 95}}
            ],
        }
        text = _format_price_changed_alert(payload)
        assert "MRP" in text and "Selling" in text


class TestPlainTextNeverBreaksOnSpecialCharacters:
    """Regression tests for a real production incident: Telegram messages
    were sent with parse_mode="Markdown", and dynamic content containing an
    unescaped/unbalanced markdown special character (_ * ` [) made
    send_message fail with "Can't parse entities" — after 5 stream-consumer
    retries the event was dropped to the DLQ. Manual per-field escaping was
    tried first and missed spot after spot (product names, then field-diff
    values, then the literal strings "in_stock"/"out_of_stock", then dict
    keys in the error-context preview) — including corrupting the very
    system-error alert reporting the failure (an unescaped underscore
    inside its own italics ate the "_" out of "product.price_changed").
    The actual fix: Telegram messages are now sent as plain text (no
    parse_mode at all, see TelegramService.send_message) — there is no
    entity parser to break, so no value, however constructed, can ever
    fail a send. These tests assert dynamic content survives completely
    unmodified (no backslashes inserted, nothing silently dropped)."""

    def test_price_changed_alert_keeps_underscores_in_product_name_verbatim(self):
        from src.alerts.handlers import _format_price_changed_alert

        payload = {
            "product_name": "18k_Gold Statement Ring",
            "price_changes": [
                {"country": "AU", "old": {"mrp": 100, "sellingPrice": 90}, "new": {"mrp": 100, "sellingPrice": 95}}
            ],
        }
        text = _format_price_changed_alert(payload)
        assert "18k_Gold Statement Ring" in text
        assert "\\_" not in text

    def test_price_changed_alert_keeps_stock_status_strings_verbatim(self):
        """The literal production incident: 'in_stock'/'out_of_stock'
        interpolated raw into a Markdown message broke Telegram's parser
        even though the product name itself had no special characters."""
        from src.alerts.handlers import _format_price_changed_alert

        payload = {
            "product_name": "Golden Wing Bee pendants",
            "stock_changes": [
                {"country": "AU", "old": {"qty": 50, "status": "in_stock"}, "new": {"qty": 50, "status": "out_of_stock"}}
            ],
        }
        text = _format_price_changed_alert(payload)
        assert "in_stock" in text
        assert "out_of_stock" in text
        assert "\\_" not in text

    def test_price_changed_alert_keeps_field_names_and_values_verbatim(self):
        from src.alerts.handlers import _format_price_changed_alert

        payload = {
            "product_name": "Test Ring",
            "field_changes": [
                {"field": "best_seller_order", "old": "3", "new": "1"},
                {"field": "material", "old": "925 Silver", "new": "18k_Gold Plated"},
            ],
        }
        text = _format_price_changed_alert(payload)
        assert "best_seller_order" in text
        assert "18k_Gold Plated" in text
        assert "\\_" not in text

    def test_price_changed_alert_keeps_actor_email_verbatim(self):
        from src.alerts.handlers import _format_price_changed_alert

        payload = {
            "product_name": "Test Ring",
            "actor_email": "first_last@chokmoki.com",
            "field_changes": [{"field": "active", "old": "True", "new": "False"}],
        }
        text = _format_price_changed_alert(payload)
        assert "first_last@chokmoki.com" in text

    def test_system_error_alert_keeps_context_keys_and_values_verbatim(self):
        """The exact incident: a system-error alert ABOUT the price-changed
        failure carried the event type 'product.price_changed' — with the
        old Markdown-escaping approach the dict KEYS (event_type,
        stream_key, ...) were never escaped even after the VALUES were,
        which alone was still enough to break the parse."""
        from src.alerts.handlers import _format_system_error_alert

        payload = {
            "component": "stream_consumer",
            "message": "Event 'product.price_changed' dropped after 5 failed attempts",
            "context": {"event_type": "product.price_changed", "delivery_count": 5},
        }
        text = _format_system_error_alert(payload)
        assert "product.price_changed" in text
        assert "event_type=product.price_changed" in text
        assert "\\_" not in text

    def test_stock_level_alert_keeps_product_name_verbatim(self):
        from src.alerts.handlers import _format_stock_level_alert

        text = _format_stock_level_alert(
            "product.low_stock",
            {"product_name": "18k_Gold Ring", "country": "AU", "qty": 5, "threshold": 10},
        )
        assert "18k_Gold Ring" in text
        assert "\\_" not in text

    def test_contact_alert_keeps_free_text_message_verbatim(self):
        from src.alerts.handlers import _format_contact_alert

        payload = {"name": "A_B", "email": "a_b@example.com", "message": "Interested in *this* ring_set"}
        text = _format_contact_alert(payload)
        assert "A_B" in text
        assert "a_b@example.com" in text
        assert "*this* ring_set" in text
        assert "\\" not in text


class TestStockStatusDrivenAlertsPureRule:
    """Regression tests for "when I put out of stock and in stock, I don't
    get any messages": the crossing rule was purely qty-based, so an admin
    manually flipping the status dropdown WITHOUT changing qty (exactly
    what was happening in production — qty stayed 50, only status flipped)
    never triggered any alert at all. evaluate_stock_crossing is now
    status-driven first."""

    def test_status_flip_to_out_of_stock_with_unchanged_qty_alerts(self):
        from src.services.stock_alerts import STOCK_EVENT_OUT_OF_STOCK, evaluate_stock_crossing

        result = evaluate_stock_crossing(50, 50, 10, "in_stock", "out_of_stock")
        assert result == STOCK_EVENT_OUT_OF_STOCK

    def test_status_flip_to_in_stock_with_unchanged_qty_alerts_back_in_stock(self):
        from src.services.stock_alerts import STOCK_EVENT_BACK_IN_STOCK, evaluate_stock_crossing

        result = evaluate_stock_crossing(50, 50, 10, "out_of_stock", "in_stock")
        assert result == STOCK_EVENT_BACK_IN_STOCK

    def test_no_status_change_falls_back_to_qty_threshold_rule(self):
        from src.services.stock_alerts import evaluate_stock_crossing

        assert evaluate_stock_crossing(50, 45, 10, "in_stock", "in_stock") is None
        assert evaluate_stock_crossing(11, 9, 10, "in_stock", "in_stock") == "low_stock"


@pytest.mark.asyncio
class TestStockStatusDrivenAlertsProductService:
    async def test_admin_flipping_status_alone_fires_out_of_stock(self, seeded_product_high_stock):
        service = ProductService()
        new_stock = [
            {"country": "IN", "qty": 50, "status": "in_stock"},
            {"country": "AU", "qty": 15, "status": "out_of_stock"},  # qty unchanged, status flipped
            {"country": "default", "qty": 50, "status": "in_stock"},
        ]
        with patch("src.services.product_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service.update(
                str(seeded_product_high_stock.id), {"stock": new_stock}, actor_email="root@chokmoki.com"
            )
        event_types = [call.args[0] for call in mock_publish.call_args_list]
        assert "product.out_of_stock" in event_types

    async def test_admin_flipping_status_back_alone_fires_back_in_stock(self, seeded_product_high_stock):
        service = ProductService()
        # First flip to out_of_stock (qty unchanged).
        first_stock = [
            {"country": "IN", "qty": 50, "status": "in_stock"},
            {"country": "AU", "qty": 15, "status": "out_of_stock"},
            {"country": "default", "qty": 50, "status": "in_stock"},
        ]
        await service.update(str(seeded_product_high_stock.id), {"stock": first_stock}, actor_email="root@chokmoki.com")

        # Then flip back to in_stock (qty still unchanged).
        second_stock = [
            {"country": "IN", "qty": 50, "status": "in_stock"},
            {"country": "AU", "qty": 15, "status": "in_stock"},
            {"country": "default", "qty": 50, "status": "in_stock"},
        ]
        with patch("src.services.product_service.publish_alert", new_callable=AsyncMock) as mock_publish:
            await service.update(
                str(seeded_product_high_stock.id), {"stock": second_stock}, actor_email="root@chokmoki.com"
            )
        event_types = [call.args[0] for call in mock_publish.call_args_list]
        assert "product.back_in_stock" in event_types
