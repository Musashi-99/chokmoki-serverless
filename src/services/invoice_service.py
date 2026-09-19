"""On-demand, branded GST document PDFs for the admin panel.

Three document types over one layout engine:
- tax_invoice: the GST tax invoice — taxable value back-calculated from the
  GST-inclusive selling price, split CGST+SGST (customer in the seller's
  state) or IGST (any other state).
- receipt: a payment receipt — same items, plus a payment block (method,
  Razorpay payment id, amount received) instead of the tax declaration.
- bill_of_supply: same document with no tax columns/split — the format used
  when no GST is being charged on the document.

Generated lazily per request, never persisted as a file: the PDF is a pure
function of the order document + config, so regeneration is always
consistent — except the invoice NUMBER, which is assigned exactly once via
an atomic Mongo counter the first time any document is generated for an
order, then stored on the order and reused forever (GST invoice numbers
must be unique and sequential and must never change once issued).

reportlab is synchronous/CPU-bound — callers must run build_pdf via
asyncio.to_thread (the admin route does) so generation never blocks the
event loop.
"""
from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from pymongo import ReturnDocument
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as pdf_canvas

from src.config import settings
from src.database.connection import db
from src.services.discount_service import coupon_product_ids
from src.utils.money import allocate_shares, inr_to_paise, money
from src.utils.region import is_au_address, is_india_address, is_nz_address

ORDERS_COLLECTION = "orders"
COUNTERS_COLLECTION = "counters"

# --- Fonts -----------------------------------------------------------------
# reportlab's base-14 fonts (Helvetica & co.) have NO glyph for U+20B9
# RUPEE SIGN — every ₹ on a generated invoice rendered as a black box.
# Noto Sans (SIL OFL, bundled in assets/fonts/) covers it, so the whole
# document switches to it rather than mixing families: currency-adjacent
# text appears on nearly every line, and Noto renders plain ASCII
# essentially identically to Helvetica at these sizes.
#
# If registration ever fails (a packaging miss in some future deploy
# target), we fall back to the base-14 names: a document with a boxed ₹ is
# far better than no document at all.
_FONT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "assets",
    "fonts",
)


def _register_invoice_fonts() -> Tuple[str, str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont("NotoSans", os.path.join(_FONT_DIR, "NotoSans-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("NotoSans-Bold", os.path.join(_FONT_DIR, "NotoSans-Bold.ttf")))
    # No italic face is bundled — the only oblique text on these documents
    # is plain English, so it maps to the regular face.
    pdfmetrics.registerFontFamily(
        "NotoSans",
        normal="NotoSans",
        bold="NotoSans-Bold",
        italic="NotoSans",
        boldItalic="NotoSans-Bold",
    )
    return "NotoSans", "NotoSans-Bold", "NotoSans"


try:
    FONT_REGULAR, FONT_BOLD, FONT_ITALIC = _register_invoice_fonts()
    UNICODE_FONTS_AVAILABLE = True
except Exception:  # pragma: no cover - only on a broken/incomplete deploy
    FONT_REGULAR, FONT_BOLD, FONT_ITALIC = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"
    UNICODE_FONTS_AVAILABLE = False

GST_STATE_CODES = {
    "andaman and nicobar islands": "35",
    "andhra pradesh": "37",
    "arunachal pradesh": "12",
    "assam": "18",
    "bihar": "10",
    "chandigarh": "04",
    "chhattisgarh": "22",
    "dadra and nagar haveli and daman and diu": "26",
    "delhi": "07",
    "nct of delhi": "07",
    "new delhi": "07",
    "goa": "30",
    "gujarat": "24",
    "haryana": "06",
    "himachal pradesh": "02",
    "jammu and kashmir": "01",
    "jharkhand": "20",
    "karnataka": "29",
    "kerala": "32",
    "ladakh": "38",
    "lakshadweep": "31",
    "madhya pradesh": "23",
    "maharashtra": "27",
    "manipur": "14",
    "meghalaya": "17",
    "mizoram": "15",
    "nagaland": "13",
    "odisha": "21",
    "orissa": "21",
    "puducherry": "34",
    "pondicherry": "34",
    "punjab": "03",
    "rajasthan": "08",
    "sikkim": "11",
    "tamil nadu": "33",
    "telangana": "36",
    "tripura": "16",
    "uttar pradesh": "09",
    "uttarakhand": "05",
    "uttaranchal": "05",
    "west bengal": "19",
    "wb": "19",
    "tn": "33",
    "mh": "27",
    "ka": "29",
    "gj": "24",
    "up": "09",
    "dl": "07",
    "rj": "08",
    "mp": "23",
    "hr": "06",
    "pb": "03",
    "br": "10",
    "od": "21",
    "ts": "36",
    "ap": "37",
    "kl": "32",
    "as": "18",
    "jh": "20",
    "ct": "22",
    "cg": "22",
    "uk": "05",
    "ua": "05",
    "hp": "02",
    "ga": "30",
}


def _norm_state(value: str) -> str:
    return " ".join(value.replace(".", "").strip().lower().split())


def gst_state_code(value: str) -> str:
    key = _norm_state(value)
    return GST_STATE_CODES.get(key, "")


DOC_TITLES = {
    "tax_invoice": "TAX INVOICE",
    "receipt": "PAYMENT RECEIPT",
    "bill_of_supply": "BILL OF SUPPLY",
}

_ONES = (
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
)
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _two_digits(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def _three_digits(n: int) -> str:
    if n < 100:
        return _two_digits(n)
    rest = _two_digits(n % 100)
    return (_ONES[n // 100] + " Hundred" + (" " + rest if rest else "")).strip()


def amount_in_words_inr(amount: float) -> str:
    """Indian-numbering words for an INR amount (crore/lakh/thousand)."""
    paise_total = inr_to_paise(amount)
    rupees, paise = divmod(paise_total, 100)
    if rupees == 0:
        words = "Zero"
    else:
        parts: List[str] = []
        crore, rupees = divmod(rupees, 10_000_000)
        lakh, rupees = divmod(rupees, 100_000)
        thousand, hundreds = divmod(rupees, 1000)
        if crore:
            parts.append(_two_digits(crore) + " Crore")
        if lakh:
            parts.append(_two_digits(lakh) + " Lakh")
        if thousand:
            parts.append(_two_digits(thousand) + " Thousand")
        if hundreds:
            parts.append(_three_digits(hundreds))
        words = " ".join(parts)
    result = f"{words} Rupees"
    if paise:
        result += f" and {_two_digits(paise)} Paise"
    return result + " Only"


class InvoiceService:
    async def _db(self):
        return await db.get_database()

    async def get_or_assign_invoice_number(self, order_id: str) -> Tuple[str, datetime]:
        """Assign the order's permanent invoice number exactly once.

        The atomic counter ($inc on a single counters doc) guarantees
        uniqueness + sequence; the conditional order update ($exists: False
        filter) guarantees a concurrent second request can't assign a second
        number — the loser's counter increment is simply an unused gap in
        the sequence, which GST rules tolerate far better than a duplicate
        or a changed number.
        """
        database = await self._db()
        orders = database[ORDERS_COLLECTION]

        existing = await orders.find_one(
            {"order_id": order_id}, {"invoice_number": 1, "invoice_date": 1}
        )
        if existing is None:
            raise ValueError("Order not found")
        if existing.get("invoice_number"):
            return existing["invoice_number"], existing["invoice_date"]

        year = datetime.utcnow().year
        counter = await database[COUNTERS_COLLECTION].find_one_and_update(
            {"_id": f"invoice_number_{year}"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        invoice_number = f"{settings.invoice_number_prefix}-{year}-{counter['seq']:06d}"
        invoice_date = datetime.utcnow()

        claimed = await orders.find_one_and_update(
            {"order_id": order_id, "invoice_number": {"$exists": False}},
            {"$set": {"invoice_number": invoice_number, "invoice_date": invoice_date}},
            return_document=ReturnDocument.AFTER,
        )
        if claimed is None:
            # Lost the race — another request assigned one a moment ago.
            current = await orders.find_one(
                {"order_id": order_id}, {"invoice_number": 1, "invoice_date": 1}
            )
            return current["invoice_number"], current["invoice_date"]
        return invoice_number, invoice_date

    # ---- GST math -------------------------------------------------------

    def is_india_order(self, order_doc: Dict[str, Any]) -> bool:
        """GST (CGST/SGST/IGST) only applies to a domestic supply — an order
        shipping outside India is an export, which this business isn't set
        up to charge Indian GST on. Checked against the actual shipping
        address (not `region_audit.pricing_country_used`, which only picked
        a price bucket) so this can never disagree with the physical
        destination of the parcel."""
        return is_india_address((order_doc.get("shipping_address") or {}).get("country"))

    def _tax_region(self, order_doc: Dict[str, Any]) -> str:
        """Which tax regime, if any, this parcel's destination falls under:
        "IN" | "AU" | "NZ" | "default". Same address-name sets the
        storefront uses, checked against the physical shipping address for
        exactly the reasons is_india_order() documents above."""
        country = (order_doc.get("shipping_address") or {}).get("country")
        if is_india_address(country):
            return "IN"
        if is_au_address(country):
            return "AU"
        if is_nz_address(country):
            return "NZ"
        return "default"

    def is_tax_invoice_eligible(self, order_doc: Dict[str, Any]) -> bool:
        """A "Tax Invoice" is a document issued under a tax regime we're
        actually operating in. India (GST), plus Australia and New Zealand
        — both of which have a real GST regime and a configurable rate
        (0% until registration, which is still a legitimate document)."""
        return self.is_india_order(order_doc) or self._tax_region(order_doc) in ("AU", "NZ")

    def _is_intra_state(self, order_doc: Dict[str, Any]) -> bool:
        customer_state = (order_doc.get("shipping_address") or {}).get("state") or ""
        seller_state = settings.invoice_seller_state
        customer_code = gst_state_code(customer_state)
        seller_code = (settings.invoice_seller_state_code or "").strip() or gst_state_code(
            seller_state
        )
        if customer_code and seller_code:
            return customer_code == seller_code
        return _norm_state(customer_state) == _norm_state(seller_state)

    def _discount_mask(self, order_doc: Dict[str, Any], items: List[Dict[str, Any]]) -> List[bool]:
        snap = order_doc.get("applied_discount") or {}
        coupon_type = snap.get("type") if isinstance(snap, dict) else getattr(snap, "type", None)
        if not coupon_type or str(coupon_type) == "CART":
            return [True] * len(items)
        ids = set(coupon_product_ids(snap))
        return [str(item.get("product_id")) in ids for item in items]

    def _tax_lines(self, order_doc: Dict[str, Any], doc_type: str) -> List[Dict[str, Any]]:
        """Per-item rows with the GST-inclusive price decomposed into
        taxable value + tax amounts. bill_of_supply keeps the full price as
        the line value with no tax split (that's what the format means).
        Discount is allocated onto eligible lines before the GST split so
        taxable value matches what was charged.
        """
        region = self._tax_region(order_doc)
        # India's rate resolves to gst_total_percent exactly as before —
        # gst_rate_for_region("IN") IS gst_total_percent, so the domestic
        # math below is unchanged. AU/NZ get their own single flat rate
        # (0% until the business registers there); everywhere else is an
        # export with no tax to charge.
        rate = (
            Decimal(str(settings.gst_rate_for_region(region))) / Decimal("100")
            if settings.gst_enabled and region in ("IN", "AU", "NZ")
            else Decimal("0")
        )
        split_tax = doc_type != "bill_of_supply" and rate > 0
        # Only India splits the tax into CGST+SGST / IGST; AU/NZ GST is a
        # single line, so it gets its own `gst` field per row rather than
        # overloading `igst` with something that isn't an integrated tax.
        india_split = split_tax and region == "IN"
        intra = self._is_intra_state(order_doc)

        items = list(order_doc.get("items", []))
        line_totals = [
            money(Decimal(str(item.get("unit_price", 0))) * int(item.get("quantity", 1)))
            for item in items
        ]
        shares = allocate_shares(
            line_totals,
            money(order_doc.get("discount") or 0),
            self._discount_mask(order_doc, items),
        )

        rows = []
        for item, line_total, share in zip(items, line_totals, shares):
            qty = int(item.get("quantity", 1))
            unit_price = money(item.get("unit_price", 0))
            net = money(Decimal(str(line_total)) - Decimal(str(share)))
            if split_tax:
                taxable = money(Decimal(str(net)) / (Decimal("1") + rate))
                tax_amount = money(Decimal(str(net)) - Decimal(str(taxable)))
            else:
                taxable = net
                tax_amount = 0.0
            half = money(Decimal(str(tax_amount)) / Decimal("2"))
            remainder = money(Decimal(str(tax_amount)) - Decimal(str(half)))
            rows.append({
                "name": item.get("product_name", "Item"),
                "sku": item.get("product_id", ""),
                # HSN is an Indian GST classification — meaningless on an
                # AU/NZ document, which prints no HSN column at all.
                "hsn": settings.gst_hsn_code if india_split else "",
                "qty": qty,
                "unit_price": unit_price,
                "taxable": taxable,
                "cgst": half if (india_split and intra) else 0.0,
                "sgst": remainder if (india_split and intra) else 0.0,
                "igst": tax_amount if (india_split and not intra) else 0.0,
                "gst": tax_amount if (split_tax and not india_split) else 0.0,
                "gross": line_total,
                "total": net,
                "net": net,
                "discount": share,
            })
        return rows

    def _commercial_totals(self, order_doc: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, float]:
        gross = money(sum(r.get("gross", r["total"]) for r in rows))
        net_goods = money(sum(r["net"] for r in rows))
        discount = money(order_doc.get("discount") or 0)
        shipping = money(order_doc.get("shipping") or 0)
        taxable = money(sum(r["taxable"] for r in rows))
        cgst = money(sum(r["cgst"] for r in rows))
        sgst = money(sum(r["sgst"] for r in rows))
        igst = money(sum(r["igst"] for r in rows))
        gst = money(sum(r.get("gst", 0) for r in rows))
        grand = money(order_doc.get("total_amount") or 0)
        return {
            "gross": gross,
            "net_goods": net_goods,
            "discount": discount,
            "shipping": shipping,
            "taxable": taxable,
            "cgst": cgst,
            "sgst": sgst,
            "igst": igst,
            "gst": gst,
            "grand": grand,
        }

    # ---- PDF layout ------------------------------------------------------

    def build_pdf(
        self,
        order_doc: Dict[str, Any],
        *,
        doc_type: str,
        invoice_number: str,
        invoice_date: datetime,
    ) -> bytes:
        """Synchronous, CPU-bound — run via asyncio.to_thread."""
        import io

        if doc_type not in DOC_TITLES:
            raise ValueError(f"Unknown document type: {doc_type}")

        is_india = self.is_india_order(order_doc)
        tax_region = self._tax_region(order_doc)
        if doc_type == "tax_invoice" and not self.is_tax_invoice_eligible(order_doc):
            # A "Tax Invoice" is a document issued under a tax regime we
            # actually operate in (India, Australia, New Zealand) — an
            # export anywhere else has no tax to charge, so this document
            # type simply doesn't apply. The admin route rejects this
            # before ever calling build_pdf; this is the defense-in-depth
            # copy of that same check for any other caller.
            raise ValueError(
                "Tax Invoice (GST) is only issued for orders shipping within India, "
                "Australia or New Zealand — use Bill of Supply for this order."
            )
        # The amount fields on this order were resolved once, at
        # order-creation time, against whichever MarketPrice bucket actually
        # priced it (see ValidatedOrderItem.currency in order_service.py) —
        # never re-derived here from region_audit/country, so this always
        # matches what the customer was actually charged.
        currency_sym = order_doc.get("currency_symbol") or "₹"
        currency_code = (order_doc.get("currency") or "INR").upper()

        def fmt_money(value: float) -> str:
            return f"{currency_sym} {value:.2f}"

        buffer = io.BytesIO()
        page_w, page_h = A4
        c = pdf_canvas.Canvas(buffer, pagesize=A4)
        margin = 15 * mm
        y = page_h - margin

        burgundy = colors.HexColor("#6b1f2a")
        ink = colors.HexColor("#1a1a1a")
        muted = colors.HexColor("#666666")
        hairline = colors.HexColor("#cccccc")

        # -- Brand header: logo left, document title right
        logo_path = settings.invoice_logo_path
        logo_h = 22 * mm
        if os.path.exists(logo_path):
            img = ImageReader(logo_path)
            iw, ih = img.getSize()
            logo_w = logo_h * iw / ih
            c.drawImage(img, margin, y - logo_h, width=logo_w, height=logo_h, mask="auto")
        else:
            c.setFont(FONT_BOLD, 20)
            c.setFillColor(ink)
            c.drawString(margin, y - 10 * mm, settings.invoice_brand_name)

        c.setFont(FONT_BOLD, 16)
        c.setFillColor(burgundy)
        c.drawRightString(page_w - margin, y - 8 * mm, DOC_TITLES[doc_type])
        c.setFont(FONT_REGULAR, 8)
        c.setFillColor(muted)
        c.drawRightString(page_w - margin, y - 13 * mm, settings.invoice_brand_tagline.upper())
        y -= logo_h + 6 * mm

        c.setStrokeColor(hairline)
        c.setLineWidth(0.6)
        c.line(margin, y, page_w - margin, y)
        y -= 6 * mm

        # -- Three columns: Ship To | Sold By | Document details
        addr = order_doc.get("shipping_address") or {}
        col_w = (page_w - 2 * margin) / 3

        def wrap_to_width(text: str, max_width: float, font: str = FONT_REGULAR, size: float = 8) -> List[str]:
            """Wrap by measured width, never by silently cutting characters —
            a truncated order id on a legal document reads as a DIFFERENT id
            (confirmed live: the PDF printed 'f394ec0d-…-9449' while every
            other surface — Shiprocket invoice, label, alerts — showed the
            full '…-9449-1b3e18421764'). Long unbreakable tokens (UUIDs)
            split mid-token onto continuation lines instead.
            """
            segments: List[str] = []
            for word in text.split(" "):
                current = segments.pop() if segments else ""
                candidate = f"{current} {word}".strip()
                if stringWidth(candidate, font, size) <= max_width:
                    segments.append(candidate)
                    continue
                if current:
                    segments.append(current)
                # Word alone may still be too wide (e.g. a 36-char UUID) —
                # split it by characters at the measured limit.
                chunk = ""
                for ch in word:
                    if stringWidth(chunk + ch, font, size) > max_width:
                        segments.append(chunk)
                        chunk = ch
                    else:
                        chunk += ch
                if chunk:
                    segments.append(chunk)
            return segments or [""]

        def block(x: float, top: float, title: str, lines: List[str]) -> float:
            c.setFont(FONT_BOLD, 8.5)
            c.setFillColor(ink)
            c.drawString(x, top, title)
            yy = top - 4.5 * mm
            c.setFont(FONT_REGULAR, 8)
            c.setFillColor(muted)
            for line in lines:
                if not line:
                    continue
                for segment in wrap_to_width(line, col_w - 4 * mm):
                    c.drawString(x, yy, segment)
                    yy -= 3.8 * mm
            return yy

        ship_lines = [
            addr.get("full_name", ""),
            addr.get("address_line1", ""),
            addr.get("address_line2", ""),
            f"{addr.get('city', '')} {addr.get('postal_code', '')}".strip(),
            f"{addr.get('state', '')}, {addr.get('country', 'India')}",
            f"Ph: {addr.get('phone', '')}" if addr.get("phone") else "",
        ]
        seller_lines = [
            settings.invoice_seller_name,
            settings.invoice_seller_address1,
            settings.invoice_seller_address2,
            f"{settings.invoice_seller_city} {settings.invoice_seller_pincode}",
            f"{settings.invoice_seller_state}, India (State Code: {settings.invoice_seller_state_code})",
            f"GSTIN: {settings.invoice_seller_gstin}",
            f"Ph: {settings.invoice_seller_phone}",
            f"Email: {settings.invoice_seller_email}",
        ]
        created = order_doc.get("created_at")
        order_date_str = created.strftime("%d/%m/%Y") if isinstance(created, datetime) else ""
        detail_lines = [
            f"No.: {invoice_number}",
            f"Date: {invoice_date.strftime('%d/%m/%Y')}",
            f"Order: {order_doc.get('order_id', '')}",
            f"Order Date: {order_date_str}",
            f"Payment: {(order_doc.get('payment_method') or '').upper()}",
            f"AWB: {order_doc.get('awb_code')}" if order_doc.get("awb_code") else "",
            f"Place of Supply: {addr.get('state', '')}",
        ]
        bottoms = [
            block(margin, y, "SHIP TO / BILL TO:", ship_lines),
            block(margin + col_w, y, "SOLD BY:", seller_lines),
            block(margin + 2 * col_w, y, "DOCUMENT DETAILS:", detail_lines),
        ]
        y = min(bottoms) - 6 * mm

        # -- Items table
        rows = self._tax_lines(order_doc, doc_type)
        intra = self._is_intra_state(order_doc)
        tax_rate = settings.gst_rate_for_region(tax_region) if settings.gst_enabled else 0.0
        # A tax document only grows tax columns when there is actually a
        # rate to show — an AU/NZ Tax Invoice at the current 0% default is
        # a legitimate zero-tax document and renders like a plain one.
        show_tax = doc_type != "bill_of_supply" and tax_rate > 0
        show_india_tax = show_tax and is_india
        show_flat_tax = show_tax and not is_india
        if show_india_tax and intra:
            headers = ["S.No", "Product", "HSN", "Qty", f"Unit Price ({currency_sym})", "Taxable Value",
                       f"CGST ({settings.gst_cgst_percent}%)", f"SGST ({settings.gst_sgst_percent}%)", "Total (Incl. GST)"]
            widths = [0.05, 0.29, 0.07, 0.05, 0.11, 0.13, 0.10, 0.10, 0.10]
        elif show_india_tax:
            headers = ["S.No", "Product", "HSN", "Qty", f"Unit Price ({currency_sym})", "Taxable Value",
                       f"IGST ({self._fmt_rate(settings.gst_total_percent)}%)", "Total (Incl. GST)"]
            widths = [0.05, 0.34, 0.08, 0.05, 0.12, 0.14, 0.10, 0.12]
        elif show_flat_tax:
            # AU/NZ: one flat GST column, no HSN (an Indian classification).
            headers = ["S.No", "Product", "Qty", f"Unit Price ({currency_sym})", "Taxable Value",
                       f"GST ({self._fmt_rate(tax_rate)}%)", "Total (Incl. GST)"]
            widths = [0.05, 0.37, 0.06, 0.13, 0.14, 0.11, 0.14]
        else:
            headers = ["S.No", "Product", "Qty", f"Unit Price ({currency_sym})", f"Total ({currency_code})"]
            widths = [0.06, 0.52, 0.08, 0.16, 0.18]
        widths = [w * (page_w - 2 * margin) for w in widths]

        def table_row(values: List[str], yy: float, *, bold: bool = False) -> float:
            c.setFont(FONT_BOLD if bold else FONT_REGULAR, 7.5)
            c.setFillColor(ink if bold else muted)
            x = margin
            for value, w in zip(values, widths):
                c.drawString(x + 1.5 * mm, yy, str(value)[:40])
                x += w
            return yy - 6 * mm

        c.setFillColor(colors.HexColor("#f6f1ee"))
        c.rect(margin, y - 2 * mm, page_w - 2 * margin, 7 * mm, fill=1, stroke=0)
        y = table_row(headers, y, bold=True)
        c.setStrokeColor(hairline)
        c.line(margin, y + 4 * mm, page_w - margin, y + 4 * mm)

        for i, r in enumerate(rows, start=1):
            if show_india_tax and intra:
                values = [i, r["name"], r["hsn"], r["qty"], f"{r['unit_price']:.2f}",
                          f"{r['taxable']:.2f}", f"{r['cgst']:.2f}", f"{r['sgst']:.2f}", f"{r['total']:.2f}"]
            elif show_india_tax:
                values = [i, r["name"], r["hsn"], r["qty"], f"{r['unit_price']:.2f}",
                          f"{r['taxable']:.2f}", f"{r['igst']:.2f}", f"{r['total']:.2f}"]
            elif show_flat_tax:
                values = [i, r["name"], r["qty"], f"{r['unit_price']:.2f}",
                          f"{r['taxable']:.2f}", f"{r.get('gst', 0):.2f}", f"{r['total']:.2f}"]
            else:
                values = [i, r["name"], r["qty"], f"{r['unit_price']:.2f}", f"{r['total']:.2f}"]
            y = table_row(values, y)
        c.line(margin, y + 4 * mm, page_w - margin, y + 4 * mm)
        y -= 2 * mm

        # -- Totals block (right-aligned)
        totals = self._commercial_totals(order_doc, rows)
        taxable_total = totals["taxable"]
        cgst_total = totals["cgst"]
        sgst_total = totals["sgst"]
        igst_total = totals["igst"]
        shipping = totals["shipping"]
        discount = totals["discount"]
        grand_total = totals["grand"]
        subtotal = totals["gross"]

        def total_line(label: str, value: str, yy: float, *, bold: bool = False) -> float:
            c.setFont(FONT_BOLD if bold else FONT_REGULAR, 8.5 if bold else 8)
            c.setFillColor(ink if bold else muted)
            c.drawRightString(page_w - margin - 30 * mm, yy, label)
            c.drawRightString(page_w - margin, yy, value)
            return yy - 5 * mm

        y = total_line("Subtotal:", fmt_money(subtotal), y)
        if discount:
            label = "Discount:"
            snap = order_doc.get("applied_discount") or {}
            code = snap.get("code") if isinstance(snap, dict) else getattr(snap, "code", None)
            if code:
                label = f"Discount ({code}):"
            y = total_line(label, f"- {fmt_money(discount)}", y)
        if shipping:
            y = total_line("Shipping:", fmt_money(shipping), y)
        y = total_line("GRAND TOTAL:", fmt_money(grand_total), y, bold=True)
        if show_tax:
            y -= 2 * mm
            c.setFont(FONT_REGULAR, 7.5)
            c.setFillColor(muted)
            c.drawRightString(page_w - margin, y, "GST breakup (included in prices)")
            y -= 4.5 * mm
            y = total_line("Taxable Value:", fmt_money(taxable_total), y)
            if show_flat_tax:
                y = total_line(
                    f"GST @ {self._fmt_rate(tax_rate)}%:",
                    fmt_money(totals["gst"]),
                    y,
                )
            elif intra:
                y = total_line(f"CGST @ {settings.gst_cgst_percent}%:", fmt_money(cgst_total), y)
                y = total_line(f"SGST @ {settings.gst_sgst_percent}%:", fmt_money(sgst_total), y)
            else:
                y = total_line(
                    f"IGST @ {self._fmt_rate(settings.gst_total_percent)}%:",
                    fmt_money(igst_total),
                    y,
                )
        y -= 1 * mm

        c.setFont(FONT_ITALIC, 8)
        c.setFillColor(muted)
        if currency_code == "INR":
            c.drawString(margin, y, f"Amount in words: {amount_in_words_inr(grand_total)}")
        else:
            c.drawString(margin, y, f"Amount charged: {fmt_money(grand_total)} {currency_code}")
        y -= 8 * mm

        # -- Receipt payment block / tax declaration
        if doc_type == "receipt":
            c.setFont(FONT_BOLD, 8.5)
            c.setFillColor(ink)
            c.drawString(margin, y, "PAYMENT DETAILS:")
            y -= 4.5 * mm
            c.setFont(FONT_REGULAR, 8)
            c.setFillColor(muted)
            method = (order_doc.get("payment_method") or "").upper()
            paid = order_doc.get("payment_status") == "completed"
            c.drawString(margin, y, f"Method: {method}  ·  Status: {'RECEIVED' if paid else 'PENDING'}")
            y -= 4 * mm
            if order_doc.get("razorpay_payment_id"):
                c.drawString(margin, y, f"Razorpay Payment ID: {order_doc['razorpay_payment_id']}")
                y -= 4 * mm
            c.drawString(margin, y, f"Amount Received: {fmt_money(grand_total)}" if paid else "Amount Due on Delivery")
            y -= 8 * mm
        elif is_india:
            c.setFont(FONT_REGULAR, 8)
            c.setFillColor(muted)
            c.drawString(margin, y, "Whether tax is payable under reverse charge — No")
            y -= 8 * mm

        # -- Footer: declaration + signatory
        c.setStrokeColor(hairline)
        c.line(margin, y, page_w - margin, y)
        y -= 5 * mm
        c.setFont(FONT_REGULAR, 7.5)
        c.setFillColor(muted)
        c.drawString(margin, y, "Declaration: Certified that the particulars given above are true and correct.")
        c.setFont(FONT_BOLD, 8)
        c.setFillColor(ink)
        c.drawRightString(page_w - margin, y, f"For {settings.invoice_seller_name}")
        y -= 12 * mm
        c.setFont(FONT_REGULAR, 7.5)
        c.setFillColor(muted)
        c.drawRightString(page_w - margin, y, "Authorised Signatory")
        c.setFont(FONT_ITALIC, 7)
        c.drawString(
            margin, y,
            f"{settings.invoice_brand_name} · {settings.invoice_brand_tagline} · "
            f"This is a computer-generated document.",
        )

        c.showPage()
        c.save()
        return buffer.getvalue()

    @staticmethod
    def _fmt_rate(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else str(value)
