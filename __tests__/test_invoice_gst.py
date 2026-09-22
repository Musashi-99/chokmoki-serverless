from __future__ import annotations

import os
import sys

os.environ["ENVIRONMENT"] = "development"
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.invoice_service import InvoiceService, gst_state_code
from src.utils.money import money


def test_gst_state_code_aliases():
    assert gst_state_code("West Bengal") == "19"
    assert gst_state_code("WB") == "19"
    assert gst_state_code("w.b.") == "19"


def test_intra_state_matches_wb_alias(monkeypatch):
    from src.services import invoice_service

    monkeypatch.setattr(invoice_service.settings, "invoice_seller_state", "West Bengal")
    monkeypatch.setattr(invoice_service.settings, "invoice_seller_state_code", "19")
    svc = InvoiceService()
    assert svc._is_intra_state({"shipping_address": {"state": "WB"}}) is True
    assert svc._is_intra_state({"shipping_address": {"state": "Maharashtra"}}) is False


class TestIntraStateByPincode:
    """A valid 6-digit Indian PIN code is India Post's own allocation —
    stronger evidence than the free-text `state` field a customer typed —
    so it takes precedence whenever the seller's home state is West
    Bengal (state code "19"). 70-74 prefix = West Bengal, except the
    737 (Sikkim) and 744 (Andaman & Nicobar) carve-outs."""

    def _svc(self, monkeypatch):
        from src.services import invoice_service

        monkeypatch.setattr(invoice_service.settings, "invoice_seller_state", "West Bengal")
        monkeypatch.setattr(invoice_service.settings, "invoice_seller_state_code", "19")
        return InvoiceService()

    def test_kolkata_pincode_is_intra_state_even_if_state_field_is_wrong(self, monkeypatch):
        svc = self._svc(monkeypatch)
        # State field is wrong/mistyped, but the PIN (Kolkata GPO) is
        # unambiguous — PIN wins.
        assert svc._is_intra_state(
            {"shipping_address": {"state": "Maharashtra", "postal_code": "700001"}}
        ) is True

    def test_every_70_to_74_prefix_is_west_bengal_except_carve_outs(self, monkeypatch):
        svc = self._svc(monkeypatch)
        for prefix in ("700", "710", "721", "732", "743"):
            assert svc._is_intra_state(
                {"shipping_address": {"state": "", "postal_code": f"{prefix}001"}}
            ) is True

    def test_sikkim_737_prefix_is_not_west_bengal(self, monkeypatch):
        svc = self._svc(monkeypatch)
        assert svc._is_intra_state(
            {"shipping_address": {"state": "West Bengal", "postal_code": "737101"}}
        ) is False

    def test_andaman_744_prefix_is_not_west_bengal(self, monkeypatch):
        svc = self._svc(monkeypatch)
        assert svc._is_intra_state(
            {"shipping_address": {"state": "West Bengal", "postal_code": "744101"}}
        ) is False

    def test_non_wb_pincode_with_wb_state_field_is_inter_state(self, monkeypatch):
        svc = self._svc(monkeypatch)
        # Mumbai PIN, but state field says West Bengal — PIN wins, so
        # this is IGST (inter-state), not CGST+SGST.
        assert svc._is_intra_state(
            {"shipping_address": {"state": "West Bengal", "postal_code": "400001"}}
        ) is False

    def test_missing_or_invalid_pincode_falls_back_to_state_name(self, monkeypatch):
        svc = self._svc(monkeypatch)
        assert svc._is_intra_state({"shipping_address": {"state": "WB", "postal_code": ""}}) is True
        assert svc._is_intra_state(
            {"shipping_address": {"state": "WB", "postal_code": "12345"}}
        ) is True
        assert svc._is_intra_state(
            {"shipping_address": {"state": "Maharashtra", "postal_code": "not-a-pincode"}}
        ) is False

    def test_pincode_rule_does_not_apply_when_seller_is_not_west_bengal(self, monkeypatch):
        from src.services import invoice_service

        monkeypatch.setattr(invoice_service.settings, "invoice_seller_state", "Maharashtra")
        monkeypatch.setattr(invoice_service.settings, "invoice_seller_state_code", "27")
        svc = InvoiceService()
        # A Kolkata PIN here is irrelevant — seller isn't in West Bengal,
        # so this must fall back to plain state-name comparison.
        assert svc._is_intra_state(
            {"shipping_address": {"state": "Maharashtra", "postal_code": "700001"}}
        ) is True
        assert svc._is_intra_state(
            {"shipping_address": {"state": "West Bengal", "postal_code": "700001"}}
        ) is False


def test_cgst_sgst_remainder_sums_to_tax(monkeypatch):
    from src.services import invoice_service

    monkeypatch.setattr(invoice_service.settings, "gst_enabled", True)
    monkeypatch.setattr(invoice_service.settings, "gst_cgst_percent", 1.5)
    monkeypatch.setattr(invoice_service.settings, "gst_sgst_percent", 1.5)
    monkeypatch.setattr(invoice_service.settings, "invoice_seller_state", "West Bengal")
    monkeypatch.setattr(invoice_service.settings, "invoice_seller_state_code", "19")
    svc = InvoiceService()
    rows = svc._tax_lines(
        {
            "shipping_address": {"state": "West Bengal"},
            "items": [{"product_name": "Ring", "product_id": "1", "quantity": 1, "unit_price": 1500}],
        },
        "tax_invoice",
    )
    row = rows[0]
    assert money(row["cgst"] + row["sgst"]) == money(row["total"] - row["taxable"])
    assert row["igst"] == 0.0
    assert row["total"] == row["net"]
    assert row["gross"] == 1500


def test_discounted_invoice_identity(monkeypatch):
    from src.services import invoice_service
    from src.utils.money import money

    monkeypatch.setattr(invoice_service.settings, "gst_enabled", True)
    monkeypatch.setattr(invoice_service.settings, "gst_cgst_percent", 1.5)
    monkeypatch.setattr(invoice_service.settings, "gst_sgst_percent", 1.5)
    monkeypatch.setattr(invoice_service.settings, "invoice_seller_state", "West Bengal")
    monkeypatch.setattr(invoice_service.settings, "invoice_seller_state_code", "19")
    svc = InvoiceService()
    order = {
        "shipping_address": {"state": "West Bengal"},
        "items": [
            {"product_name": "Ring", "product_id": "1", "quantity": 1, "unit_price": 1000},
        ],
        "discount": 100,
        "shipping": 50,
        "total_amount": 950,
        "applied_discount": {"type": "CART", "code": "SAVE100"},
    }
    rows = svc._tax_lines(order, "tax_invoice")
    totals = svc._commercial_totals(order, rows)
    assert totals["gross"] == 1000
    assert totals["discount"] == 100
    assert totals["net_goods"] == 900
    assert totals["shipping"] == 50
    assert money(totals["gross"] - totals["discount"] + totals["shipping"]) == totals["grand"]
    assert money(totals["net_goods"] + totals["shipping"]) == totals["grand"]
    assert money(totals["taxable"] + totals["cgst"] + totals["sgst"]) == totals["net_goods"]
    assert rows[0]["total"] == rows[0]["net"]
    assert rows[0]["gross"] == 1000


def test_product_coupon_gst_only_on_eligible_line(monkeypatch):
    from src.services import invoice_service

    monkeypatch.setattr(invoice_service.settings, "gst_enabled", True)
    monkeypatch.setattr(invoice_service.settings, "gst_cgst_percent", 1.5)
    monkeypatch.setattr(invoice_service.settings, "gst_sgst_percent", 1.5)
    monkeypatch.setattr(invoice_service.settings, "invoice_seller_state", "West Bengal")
    monkeypatch.setattr(invoice_service.settings, "invoice_seller_state_code", "19")
    svc = InvoiceService()
    order = {
        "shipping_address": {"state": "West Bengal"},
        "items": [
            {"product_name": "Ring", "product_id": "ring", "quantity": 1, "unit_price": 2000},
            {"product_name": "Chain", "product_id": "chain", "quantity": 1, "unit_price": 1000},
        ],
        "discount": 200,
        "shipping": 0,
        "total_amount": 2800,
        "applied_discount": {"type": "PRODUCT", "code": "RING10", "product_ids": ["ring"]},
    }
    rows = svc._tax_lines(order, "tax_invoice")
    totals = svc._commercial_totals(order, rows)
    assert rows[0]["discount"] == 200
    assert rows[1]["discount"] == 0
    assert rows[0]["net"] == 1800
    assert rows[1]["net"] == 1000
    assert totals["net_goods"] == 2800
    assert money(totals["taxable"] + totals["cgst"] + totals["sgst"]) == totals["net_goods"]
