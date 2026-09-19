from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ENVIRONMENT"] = "development"
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "secret")

from src.models.coupon import Coupon, DiscountIndicator, DiscountType
from src.models.order import OrderCreateInput, RegionAudit
from src.models.product import MarketPrice
from src.services.discount_service import CouponService, compute_discount
from src.services.order_service import OrderService


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def setex(self, key: str, ttl: int, value: str):
        self.store[key] = value
        self.ttls[key] = ttl

    async def set(self, key: str, value: str, keepttl: bool = False, **_kwargs):
        self.store[key] = value
        return True


def _catalog_product(product_id: str = "p1", price: float = 2000):
    return SimpleNamespace(
        id=product_id,
        name="Ring",
        price_inr=price,
        active=True,
        product_variants=[],
        # No multi-region prices configured — order_service falls back to
        # price_inr for these coupon/pricing-math tests, same as before
        # multi-region existed.
        prices=[],
    )


def _cart_percent_coupon(code: str = "SAVE10", amount: float = 10) -> Coupon:
    return Coupon(
        code=code,
        type=DiscountType.CART,
        amount=amount,
        indicator=DiscountIndicator.PERCENT,
        active=True,
        product_ids=None,
    )


def _product_coupon(product_id: str = None, code: str = "PROD10", product_ids=None) -> Coupon:
    return Coupon(
        code=code,
        type=DiscountType.PRODUCT,
        amount=10,
        indicator=DiscountIndicator.PERCENT,
        active=True,
        product_ids=product_ids if product_ids is not None else [product_id],
    )


def _order_payload(**overrides):
    payload = {
        "shippingAddress": {
            "email": "x@test.com",
            "full_name": "X",
            "phone": "9999999999",
            "address_line1": "a",
            "address_line2": "",
            "city": "c",
            "state": "s",
            "postal_code": "1",
            "country": "IN",
            "is_default": False,
        },
        "items": [
            {
                "productId": "p1",
                "productName": "Ring",
                "variant": {"default": "default"},
                "quantity": 1,
                "price": 2000,
                "total": 2000,
            }
        ],
        "specialMessage": "",
        "pricing": {"subtotal": 2000, "discount": 0, "shipping": 0, "total": 2000},
        "userEmail": "x@test.com",
        "timestamp": "2026-08-15T00:00:00Z",
        "paymentMethod": "cod",
    }
    payload.update(overrides)
    return payload


async def _run_guarded(*, fn, **_kwargs):
    return await fn()


@contextmanager
def order_pipeline_mocks(coupon=None, product=None, region_audit=None):
    product = product or _catalog_product()
    mock_ps = MagicMock()
    mock_ps.get_by_id = AsyncMock(return_value=product)

    orders_col = AsyncMock()
    orders_col.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=ObjectId()))
    logs_col = AsyncMock()
    logs_col.insert_one = AsyncMock()
    database = MagicMock()
    database.__getitem__ = MagicMock(
        side_effect=lambda name: orders_col if name == "orders" else logs_col
    )

    fake_redis = FakeRedis()

    with ExitStack() as stack:
        stack.enter_context(patch("src.services.order_service.ProductService", return_value=mock_ps))
        stack.enter_context(
            patch.object(CouponService, "get_by_code", new_callable=AsyncMock, return_value=coupon)
        )
        inv_cls = stack.enter_context(patch("src.services.order_service.InventoryService"))
        inv_cls.return_value.commit_items = AsyncMock()
        inv_cls.return_value.reserve_for_order = AsyncMock()
        # Multi-region gating (order_service._resolve_region) always resolves
        # to a real GeoIP lookup otherwise, which has no network access in
        # tests and would fall through to "default" — rejected for the
        # razorpay/initiate_order tests below by PREPAID_ENABLED_COUNTRIES=IN.
        # These tests are about coupon/pricing math, not region resolution,
        # so pin it to India.
        stack.enter_context(
            patch.object(
                OrderService,
                "_resolve_region",
                new_callable=AsyncMock,
                return_value=region_audit
                or RegionAudit(pricing_country_used="IN", selected_country="IN"),
            )
        )
        stack.enter_context(
            patch(
                "src.database.connection.db.get_database",
                new_callable=AsyncMock,
                return_value=database,
            )
        )
        stack.enter_context(
            patch(
                "src.services.order_service.FraudDetectionService.evaluate",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(action="allow", model_dump=lambda: {}),
            )
        )
        stack.enter_context(
            patch(
                "src.services.order_service.FraudEnrichmentService.mark_order_completed",
                new_callable=AsyncMock,
            )
        )
        stack.enter_context(
            patch(
                "src.services.order_service.redis_client.get_client",
                new_callable=AsyncMock,
                return_value=fake_redis,
            )
        )
        razorpay_cls = stack.enter_context(patch("src.services.order_service.RazorpayService"))
        razorpay_cls.return_value.create_order.return_value = SimpleNamespace(id="rzp_order_1")
        stack.enter_context(patch("src.services.order_service.call_guarded", new=_run_guarded))
        recon_cls = stack.enter_context(
            patch("src.services.order_service.PaymentReconciliationService")
        )
        recon_cls.return_value.record_attempt = AsyncMock()
        yield SimpleNamespace(
            orders=orders_col,
            logs=logs_col,
            razorpay=razorpay_cls,
            redis=fake_redis,
            inventory=inv_cls,
        )


class TestOrderCouponPricing:
    @pytest.mark.asyncio
    async def test_cod_create_cart_percent_persists_snapshot(self):
        coupon = _cart_percent_coupon()
        expected_computed, expected_total, expected_subtotal = compute_discount(
            [SimpleNamespace(product_id="p1", total_price=2000)], coupon
        )
        order_data = OrderCreateInput(**_order_payload(couponCode="SAVE10"))

        with order_pipeline_mocks(coupon=coupon) as mocks:
            order = await OrderService().create(order_data)

        mocks.orders.insert_one.assert_awaited_once()
        doc = mocks.orders.insert_one.call_args.args[0]
        assert doc["subtotal"] == expected_subtotal
        assert doc["discount"] == expected_computed
        assert doc["total_amount"] == expected_total
        assert doc["applied_discount"]["code"] == "SAVE10"
        assert order.applied_discount is not None
        assert order.applied_discount.code == "SAVE10"
        assert order.applied_discount.product_ids is None
        assert order.discount == expected_computed
        assert order.total_amount == expected_total

    @pytest.mark.asyncio
    async def test_initiate_order_razorpay_uses_discounted_total(self):
        coupon = _cart_percent_coupon()
        expected_computed, expected_total, _ = compute_discount(
            [SimpleNamespace(product_id="p1", total_price=2000)], coupon
        )
        order_data = OrderCreateInput(
            **_order_payload(couponCode="SAVE10", paymentMethod="razorpay")
        )

        with order_pipeline_mocks(coupon=coupon) as mocks:
            result = await OrderService().initiate_order(order_data)

        mocks.razorpay.return_value.create_order.assert_called_once()
        assert mocks.razorpay.return_value.create_order.call_args.kwargs["amount"] == expected_total
        assert result.amount == expected_total
        pending_raw = next(
            value for key, value in mocks.redis.store.items() if key.startswith("pending_order:")
        )
        pending = json.loads(pending_raw)
        assert pending["discount"] == expected_computed
        assert pending["total_amount"] == expected_total
        assert pending["applied_discount"]["code"] == "SAVE10"

    @pytest.mark.asyncio
    async def test_invalid_code_raises_and_does_not_insert(self):
        order_data = OrderCreateInput(**_order_payload(couponCode="NOPE"))

        with order_pipeline_mocks(coupon=None) as mocks:
            with pytest.raises(ValueError, match="Invalid coupon"):
                await OrderService().create(order_data)

        mocks.orders.insert_one.assert_not_called()
        mocks.inventory.return_value.commit_items.assert_not_called()

    @pytest.mark.asyncio
    async def test_product_coupon_wrong_product_raises(self):
        coupon = _product_coupon(product_id="other")
        order_data = OrderCreateInput(**_order_payload(couponCode="PROD10"))

        with order_pipeline_mocks(coupon=coupon) as mocks:
            with pytest.raises(ValueError, match="Coupon does not apply to this cart"):
                await OrderService().create(order_data)

        mocks.orders.insert_one.assert_not_called()

    @pytest.mark.asyncio
    async def test_product_coupon_persists_product_id_on_snapshot(self):
        coupon = _product_coupon(product_id="p1")
        order_data = OrderCreateInput(**_order_payload(couponCode="PROD10"))

        with order_pipeline_mocks(coupon=coupon) as mocks:
            order = await OrderService().create(order_data)

        doc = mocks.orders.insert_one.call_args.args[0]
        assert doc["applied_discount"]["code"] == "PROD10"
        assert doc["applied_discount"]["product_ids"] == ["p1"]
        assert doc["discount"] == 200
        assert order.applied_discount.product_ids == ["p1"]

    @pytest.mark.asyncio
    async def test_multi_product_coupon_persists_product_ids_on_snapshot(self):
        coupon = _product_coupon(product_ids=["p1", "p2"])
        order_data = OrderCreateInput(**_order_payload(couponCode="PROD10"))

        with order_pipeline_mocks(coupon=coupon) as mocks:
            order = await OrderService().create(order_data)

        doc = mocks.orders.insert_one.call_args.args[0]
        assert doc["applied_discount"]["product_ids"] == ["p1", "p2"]
        assert doc["discount"] == 200
        assert order.applied_discount.product_ids == ["p1", "p2"]

    @pytest.mark.asyncio
    async def test_admin_create_ignores_top_level_discount_without_code(self):
        with order_pipeline_mocks(coupon=None) as mocks:
            await OrderService().create_from_admin(
                {**_order_payload(), "discount": 9999}
            )

        doc = mocks.orders.insert_one.call_args.args[0]
        assert "applied_discount" not in doc
        assert doc["discount"] == 0
        assert doc["total_amount"] == 2000

    @pytest.mark.asyncio
    async def test_no_coupon_omits_applied_discount_and_zero_discount(self):
        order_data = OrderCreateInput(**_order_payload())

        with order_pipeline_mocks(coupon=None) as mocks:
            order = await OrderService().create(order_data)

        doc = mocks.orders.insert_one.call_args.args[0]
        assert "applied_discount" not in doc
        assert doc["discount"] == 0
        assert doc["total_amount"] == 2000
        assert order.applied_discount is None
        assert order.discount == 0

    @pytest.mark.asyncio
    async def test_client_pricing_discount_ignored_without_code(self):
        order_data = OrderCreateInput(
            **_order_payload(
                pricing={"subtotal": 2000, "discount": 9999, "shipping": 0, "total": 1}
            )
        )

        with order_pipeline_mocks(coupon=None) as mocks:
            order = await OrderService().create(order_data)

        doc = mocks.orders.insert_one.call_args.args[0]
        assert "applied_discount" not in doc
        assert doc["discount"] == 0
        assert doc["total_amount"] == 2000
        assert order.discount == 0
        assert order.total_amount == 2000

    @pytest.mark.asyncio
    async def test_admin_create_cart_percent_persists_snapshot(self):
        coupon = _cart_percent_coupon()
        expected_computed, expected_total, expected_subtotal = compute_discount(
            [SimpleNamespace(product_id="p1", total_price=2000)], coupon
        )

        with order_pipeline_mocks(coupon=coupon) as mocks:
            await OrderService().create_from_admin(_order_payload(coupon_code="SAVE10"))

        mocks.orders.insert_one.assert_awaited_once()
        doc = mocks.orders.insert_one.call_args.args[0]
        assert doc["subtotal"] == expected_subtotal
        assert doc["discount"] == expected_computed
        assert doc["total_amount"] == expected_total
        assert doc["applied_discount"]["code"] == "SAVE10"

    @pytest.mark.asyncio
    async def test_admin_create_ignores_payload_discount_without_code(self):
        with order_pipeline_mocks(coupon=None) as mocks:
            await OrderService().create_from_admin(
                _order_payload(
                    pricing={"subtotal": 2000, "discount": 9999, "shipping": 0, "total": 1}
                )
            )

        doc = mocks.orders.insert_one.call_args.args[0]
        assert "applied_discount" not in doc
        assert doc["discount"] == 0
        assert doc["total_amount"] == 2000

    @pytest.mark.asyncio
    async def test_initiate_order_without_coupon_omits_applied_discount(self):
        order_data = OrderCreateInput(**_order_payload(paymentMethod="razorpay"))

        with order_pipeline_mocks(coupon=None) as mocks:
            result = await OrderService().initiate_order(order_data)

        mocks.razorpay.return_value.create_order.assert_called_once()
        assert mocks.razorpay.return_value.create_order.call_args.kwargs["amount"] == 2000
        assert result.amount == 2000
        pending_raw = next(
            value for key, value in mocks.redis.store.items() if key.startswith("pending_order:")
        )
        pending = json.loads(pending_raw)
        assert pending["discount"] == 0
        assert pending["total_amount"] == 2000
        assert "applied_discount" not in pending


class TestBuildOrderQueryCoupon:
    def test_coupon_filter_uppercases_exact_code(self):
        query = OrderService()._build_order_query(coupon="krish20")
        assert query["applied_discount.code"] == "KRISH20"

    def test_search_includes_coupon_code(self):
        query = OrderService()._build_order_query(search="KRISH20")
        fields = [clause for or_item in query["$or"] for clause in or_item]
        assert "applied_discount.code" in fields

    def test_coupon_and_search_and_together(self):
        query = OrderService()._build_order_query(search="priya", coupon="KRISH20")
        assert query["applied_discount.code"] == "KRISH20"
        assert "$or" in query

    def test_country_filter_matches_pricing_country_used(self):
        query = OrderService()._build_order_query(country="au")
        assert query["region_audit.pricing_country_used"] == "AU"

    def test_country_filter_default_bucket_stays_lowercase(self):
        query = OrderService()._build_order_query(country="default")
        assert query["region_audit.pricing_country_used"] == "default"


class TestRegionCheckoutPolicy:
    """COD needs no payment gateway, so it's allowed from every region —
    including a non-Indian shipping address, since those orders are just
    fulfilled manually by admins (Shiprocket doesn't ship internationally
    yet). Razorpay (pay online now) is India-only for both pricing region
    and shipping address, since a prepaid order has no manual-collection
    fallback the way COD does."""

    @pytest.mark.asyncio
    async def test_cod_order_allowed_from_australia(self):
        order_data = OrderCreateInput(**_order_payload(paymentMethod="cod"))
        au_region = RegionAudit(pricing_country_used="AU", selected_country="AU")

        with order_pipeline_mocks(region_audit=au_region) as mocks:
            order = await OrderService().create(order_data)

        mocks.orders.insert_one.assert_awaited_once()
        assert order.order_id

    @pytest.mark.asyncio
    async def test_razorpay_rejected_from_australia(self):
        order_data = OrderCreateInput(**_order_payload(paymentMethod="razorpay"))
        au_region = RegionAudit(pricing_country_used="AU", selected_country="AU")

        with order_pipeline_mocks(region_audit=au_region):
            with pytest.raises(ValueError, match="Online prepaid payment"):
                await OrderService().initiate_order(order_data)

    @pytest.mark.asyncio
    async def test_cod_order_allowed_with_non_india_shipping_address(self):
        order_data = OrderCreateInput(
            **_order_payload(
                paymentMethod="cod",
                shippingAddress={
                    "email": "x@test.com",
                    "full_name": "X",
                    "phone": "9999999999",
                    "address_line1": "a",
                    "address_line2": "",
                    "city": "c",
                    "state": "s",
                    "postal_code": "1",
                    "country": "Australia",
                    "is_default": False,
                },
            )
        )

        with order_pipeline_mocks() as mocks:
            order = await OrderService().create(order_data)

        mocks.orders.insert_one.assert_awaited_once()
        assert order.order_id

    @pytest.mark.asyncio
    async def test_cod_order_from_australia_region_allowed_with_non_india_shipping_address(self):
        order_data = OrderCreateInput(
            **_order_payload(
                paymentMethod="cod",
                shippingAddress={
                    "email": "x@test.com",
                    "full_name": "X",
                    "phone": "9999999999",
                    "address_line1": "a",
                    "address_line2": "",
                    "city": "c",
                    "state": "s",
                    "postal_code": "1",
                    "country": "Australia",
                    "is_default": False,
                },
            )
        )
        au_region = RegionAudit(pricing_country_used="AU", selected_country="AU")

        with order_pipeline_mocks(region_audit=au_region) as mocks:
            order = await OrderService().create(order_data)

        mocks.orders.insert_one.assert_awaited_once()
        assert order.order_id

    @pytest.mark.asyncio
    async def test_razorpay_rejected_with_non_india_shipping_address_even_from_india_region(self):
        order_data = OrderCreateInput(
            **_order_payload(
                paymentMethod="razorpay",
                shippingAddress={
                    "email": "x@test.com",
                    "full_name": "X",
                    "phone": "9999999999",
                    "address_line1": "a",
                    "address_line2": "",
                    "city": "c",
                    "state": "s",
                    "postal_code": "1",
                    "country": "Australia",
                    "is_default": False,
                },
            )
        )

        with order_pipeline_mocks():
            with pytest.raises(ValueError, match="only ship within India"):
                await OrderService().initiate_order(order_data)


class TestOrderCurrencyPersistence:
    """The real bug this class guards against: an Australian customer's
    order total showed up with a rupee symbol on the Thank You page,
    because the currency actually charged was never stored on the order at
    all — everything downstream just assumed INR. Each order must persist
    the currency its items were actually resolved against, not the pricing
    region or a hardcoded default."""

    @pytest.mark.asyncio
    async def test_cod_order_from_australia_persists_the_products_resolved_currency(self):
        product = _catalog_product()
        product.prices = [
            MarketPrice(country="default", sym="$", currency="USD", mrp=30, sellingPrice=25)
        ]
        au_region = RegionAudit(pricing_country_used="AU", selected_country="AU")
        order_data = OrderCreateInput(**_order_payload(paymentMethod="cod"))

        with order_pipeline_mocks(product=product, region_audit=au_region):
            order = await OrderService().create(order_data)

        assert order.currency == "USD"
        assert order.currency_symbol == "$"
        assert order.items[0].currency == "USD"
        assert order.items[0].sym == "$"

    @pytest.mark.asyncio
    async def test_cod_order_from_india_persists_inr(self):
        order_data = OrderCreateInput(**_order_payload(paymentMethod="cod"))

        with order_pipeline_mocks():
            order = await OrderService().create(order_data)

        assert order.currency == "INR"
        assert order.currency_symbol == "₹"

    @pytest.mark.asyncio
    async def test_admin_manual_order_always_persists_inr(self):
        product = _catalog_product()
        with order_pipeline_mocks(product=product):
            order = await OrderService().create_from_admin({
                "user_email": "x@test.com",
                "shipping_address": {
                    "full_name": "X", "phone": "9999999999",
                    "address_line1": "a", "city": "c",
                    "state": "s", "postal_code": "1", "country": "India",
                },
                "items": [{"product_id": "p1", "quantity": 1}],
            })

        assert order.currency == "INR"
        assert order.currency_symbol == "₹"


class TestInvoiceGstCountryGating:
    """CGST/SGST/IGST is a domestic-supply tax — an order that ships outside
    India is an export, which isn't set up to charge Indian GST. A "Tax
    Invoice" document (and any Indian GST split on a "receipt") must never
    be produced for one of those orders, regardless of what doc_type an
    admin requests. Australia and New Zealand are a deliberate exception:
    both have their own GST regimes, so those shipping countries are
    tax-invoice-eligible too (via GST_AU_PERCENT/GST_NZ_PERCENT, 0% by
    default) — see TestAuNzTaxInvoice below. A genuinely non-taxable
    country (e.g. the US) must still be rejected for "tax_invoice"."""

    def _order_doc(self, *, country: str, currency: str = "INR", sym: str = "₹"):
        return {
            "order_id": "o1",
            "shipping_address": {
                "full_name": "X", "phone": "1", "address_line1": "a",
                "city": "c", "state": "Victoria", "postal_code": "1",
                "country": country,
            },
            "items": [
                {"product_id": "p1", "product_name": "Ring", "quantity": 1, "unit_price": 25.0, "total_price": 25.0}
            ],
            "subtotal": 25.0,
            "discount": 0,
            "shipping": 0,
            "total_amount": 25.0,
            "currency": currency,
            "currency_symbol": sym,
            "payment_method": "cod",
            "payment_status": "pending",
            "created_at": None,
        }

    def test_is_india_order_true_for_india_address(self):
        from src.services.invoice_service import InvoiceService

        assert InvoiceService().is_india_order(self._order_doc(country="India")) is True

    def test_is_india_order_false_for_australia_address(self):
        from src.services.invoice_service import InvoiceService

        assert InvoiceService().is_india_order(self._order_doc(country="Australia")) is False

    def test_tax_invoice_raises_for_non_taxable_order(self):
        from src.services.invoice_service import InvoiceService

        service = InvoiceService()
        with pytest.raises(ValueError, match="India, Australia"):
            service.build_pdf(
                self._order_doc(country="United States", currency="USD", sym="$"),
                doc_type="tax_invoice",
                invoice_number="INV-2026-000001",
                invoice_date=__import__("datetime").datetime.utcnow(),
            )

    def test_bill_of_supply_renders_in_the_orders_actual_currency_for_non_india_order(self):
        from src.services.invoice_service import InvoiceService

        service = InvoiceService()
        pdf_bytes = service.build_pdf(
            self._order_doc(country="Australia", currency="USD", sym="$"),
            doc_type="bill_of_supply",
            invoice_number="INV-2026-000002",
            invoice_date=__import__("datetime").datetime.utcnow(),
        )
        assert pdf_bytes[:4] == b"%PDF"

    def test_tax_lines_split_no_tax_for_non_india_order_even_on_receipt(self):
        from src.services.invoice_service import InvoiceService

        service = InvoiceService()
        rows = service._tax_lines(self._order_doc(country="Australia", currency="USD", sym="$"), "receipt")
        assert all(r["cgst"] == 0 and r["sgst"] == 0 and r["igst"] == 0 for r in rows)
