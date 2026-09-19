"""AU/NZ Tax Invoice support: a configurable GST rate (0% by default),
completely independent of India's CGST/SGST/IGST math, which these tests
also pin down as untouched.
"""
from __future__ import annotations

import os
import sys

os.environ["ENVIRONMENT"] = "development"
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime

import pytest

from src.services.invoice_service import InvoiceService


def _order_doc(*, country: str, currency: str = "AUD", sym: str = "$", state: str = "Victoria"):
    return {
        "order_id": "o1",
        "shipping_address": {
            "full_name": "X", "phone": "1", "address_line1": "a",
            "city": "c", "state": state, "postal_code": "1",
            "country": country,
        },
        "items": [
            {"product_id": "p1", "product_name": "Ring", "quantity": 1, "unit_price": 100.0, "total_price": 100.0}
        ],
        "subtotal": 100.0,
        "discount": 0,
        "shipping": 0,
        "total_amount": 100.0,
        "currency": currency,
        "currency_symbol": sym,
        "payment_method": "cod",
        "payment_status": "pending",
        "created_at": None,
    }


class TestTaxRegionDetection:
    def test_tax_region_india(self):
        assert InvoiceService()._tax_region(_order_doc(country="India")) == "IN"

    def test_tax_region_australia(self):
        assert InvoiceService()._tax_region(_order_doc(country="Australia")) == "AU"

    def test_tax_region_au_abbreviation(self):
        assert InvoiceService()._tax_region(_order_doc(country="AU")) == "AU"

    def test_tax_region_new_zealand(self):
        assert InvoiceService()._tax_region(_order_doc(country="New Zealand")) == "NZ"

    def test_tax_region_nz_abbreviation(self):
        assert InvoiceService()._tax_region(_order_doc(country="nz")) == "NZ"

    def test_tax_region_default_for_untaxed_country(self):
        assert InvoiceService()._tax_region(_order_doc(country="United States")) == "default"


class TestTaxInvoiceEligibility:
    def test_india_is_eligible(self):
        assert InvoiceService().is_tax_invoice_eligible(_order_doc(country="India")) is True

    def test_australia_is_eligible(self):
        assert InvoiceService().is_tax_invoice_eligible(_order_doc(country="Australia")) is True

    def test_new_zealand_is_eligible(self):
        assert InvoiceService().is_tax_invoice_eligible(_order_doc(country="New Zealand")) is True

    def test_united_states_is_not_eligible(self):
        assert InvoiceService().is_tax_invoice_eligible(_order_doc(country="United States")) is False


class TestAuNzZeroPercentDefault:
    """GST_AU_PERCENT / GST_NZ_PERCENT default to 0.0 — an AU/NZ Tax
    Invoice today is a legitimate zero-tax document, not an error."""

    def test_au_tax_invoice_builds_at_default_zero_rate(self):
        service = InvoiceService()
        pdf_bytes = service.build_pdf(
            _order_doc(country="Australia"),
            doc_type="tax_invoice",
            invoice_number="INV-2026-000010",
            invoice_date=datetime.datetime.utcnow(),
        )
        assert pdf_bytes[:4] == b"%PDF"

    def test_au_tax_lines_have_zero_gst_at_default_rate(self):
        rows = InvoiceService()._tax_lines(_order_doc(country="Australia"), "tax_invoice")
        assert all(r["gst"] == 0 and r["cgst"] == 0 and r["sgst"] == 0 and r["igst"] == 0 for r in rows)

    def test_nz_tax_invoice_builds_at_default_zero_rate(self):
        service = InvoiceService()
        pdf_bytes = service.build_pdf(
            _order_doc(country="New Zealand", currency="NZD"),
            doc_type="tax_invoice",
            invoice_number="INV-2026-000011",
            invoice_date=datetime.datetime.utcnow(),
        )
        assert pdf_bytes[:4] == b"%PDF"


class TestAuNzConfiguredNonZeroRate:
    """When GST_AU_PERCENT/GST_NZ_PERCENT are set above zero, the GST line
    is a single flat amount — never split into cgst/sgst/igst, which stay
    India-only fields."""

    def test_au_ten_percent_gst_line_computed(self, monkeypatch):
        from src.services import invoice_service

        monkeypatch.setattr(invoice_service.settings, "gst_au_percent", 10.0)
        rows = InvoiceService()._tax_lines(_order_doc(country="Australia"), "tax_invoice")
        assert len(rows) == 1
        row = rows[0]
        # net line total is 100.00, GST-inclusive at 10% -> taxable ~90.91, gst ~9.09
        assert row["gst"] == pytest.approx(9.09, abs=0.01)
        assert row["cgst"] == 0
        assert row["sgst"] == 0
        assert row["igst"] == 0
        assert row["taxable"] == pytest.approx(90.91, abs=0.01)

    def test_nz_fifteen_percent_gst_line_computed(self, monkeypatch):
        from src.services import invoice_service

        monkeypatch.setattr(invoice_service.settings, "gst_nz_percent", 15.0)
        rows = InvoiceService()._tax_lines(_order_doc(country="New Zealand", currency="NZD"), "tax_invoice")
        row = rows[0]
        assert row["gst"] == pytest.approx(13.04, abs=0.01)
        assert row["cgst"] == 0 and row["sgst"] == 0 and row["igst"] == 0

    def test_au_nonzero_rate_still_builds_valid_pdf(self, monkeypatch):
        from src.services import invoice_service

        monkeypatch.setattr(invoice_service.settings, "gst_au_percent", 10.0)
        pdf_bytes = InvoiceService().build_pdf(
            _order_doc(country="Australia"),
            doc_type="tax_invoice",
            invoice_number="INV-2026-000012",
            invoice_date=datetime.datetime.utcnow(),
        )
        assert pdf_bytes[:4] == b"%PDF"

    def test_bill_of_supply_has_no_tax_even_at_nonzero_au_rate(self, monkeypatch):
        from src.services import invoice_service

        monkeypatch.setattr(invoice_service.settings, "gst_au_percent", 10.0)
        rows = InvoiceService()._tax_lines(_order_doc(country="Australia"), "bill_of_supply")
        assert all(r["gst"] == 0 for r in rows)


class TestIndiaGstUntouchedByAuNzChange:
    """The whole point of adding AU/NZ support: India's CGST/SGST/IGST
    math must be byte-for-byte identical to before, regardless of what
    GST_AU_PERCENT/GST_NZ_PERCENT are configured to."""

    def test_india_intra_state_still_splits_cgst_sgst(self, monkeypatch):
        from src.services import invoice_service

        monkeypatch.setattr(invoice_service.settings, "gst_au_percent", 25.0)
        monkeypatch.setattr(invoice_service.settings, "gst_nz_percent", 25.0)
        monkeypatch.setattr(invoice_service.settings, "invoice_seller_state", "West Bengal")
        monkeypatch.setattr(invoice_service.settings, "invoice_seller_state_code", "19")
        rows = InvoiceService()._tax_lines(_order_doc(country="India", currency="INR", sym="₹", state="West Bengal"), "tax_invoice")
        row = rows[0]
        assert row["cgst"] > 0
        assert row["sgst"] > 0
        assert row["igst"] == 0
        assert row["gst"] == 0

    def test_india_inter_state_still_splits_igst(self, monkeypatch):
        from src.services import invoice_service

        monkeypatch.setattr(invoice_service.settings, "gst_au_percent", 25.0)
        monkeypatch.setattr(invoice_service.settings, "invoice_seller_state", "West Bengal")
        monkeypatch.setattr(invoice_service.settings, "invoice_seller_state_code", "19")
        rows = InvoiceService()._tax_lines(_order_doc(country="India", currency="INR", sym="₹", state="Maharashtra"), "tax_invoice")
        row = rows[0]
        assert row["igst"] > 0
        assert row["cgst"] == 0
        assert row["sgst"] == 0
        assert row["gst"] == 0

    def test_india_hsn_still_populated_au_does_not_get_one(self):
        india_rows = InvoiceService()._tax_lines(_order_doc(country="India", currency="INR", sym="₹"), "tax_invoice")
        au_rows = InvoiceService()._tax_lines(_order_doc(country="Australia"), "tax_invoice")
        assert india_rows[0]["hsn"] != ""
        assert au_rows[0]["hsn"] == ""


class TestGstRateForRegionHelper:
    def test_gst_rate_for_region_in_is_gst_total_percent(self):
        from src.config import settings

        assert settings.gst_rate_for_region("IN") == settings.gst_total_percent

    def test_gst_rate_for_region_au_defaults_zero(self):
        from src.config import settings

        assert settings.gst_rate_for_region("AU") == 0.0

    def test_gst_rate_for_region_nz_defaults_zero(self):
        from src.config import settings

        assert settings.gst_rate_for_region("NZ") == 0.0

    def test_gst_rate_for_region_unknown_is_zero(self):
        from src.config import settings

        assert settings.gst_rate_for_region("default") == 0.0
