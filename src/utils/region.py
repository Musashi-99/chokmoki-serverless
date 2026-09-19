"""Shared "is this shipping address actually in India?" check — used
wherever a decision depends on the physical destination of a parcel
(shipping eligibility, GST document type), as opposed to `pricing_country_used`
which only decides which MarketPrice bucket to charge. Kept in one place so
order_service.py and invoice_service.py can't drift apart on what counts as
"India" (a stray "Bharat" or lowercase "in" from an old address book entry
shouldn't silently pass one check and fail the other).
"""

from __future__ import annotations

from typing import Optional

INDIA_ADDRESS_NAMES = {"india", "in", "bharat"}
# Australia / New Zealand both run real GST regimes, so an order shipping
# there can carry a genuine Tax Invoice (at whatever rate GST_AU_PERCENT /
# GST_NZ_PERCENT is configured to — 0% until the business actually
# registers there). Deliberately the same shape as INDIA_ADDRESS_NAMES: a
# tiny hardcoded set the storefront mirrors exactly, rather than a trip
# through geo_provider's ISO mapper, so the two can't drift.
AUSTRALIA_ADDRESS_NAMES = {"australia", "au"}
NEW_ZEALAND_ADDRESS_NAMES = {"new zealand", "nz"}


def is_india_address(shipping_country: Optional[str]) -> bool:
    return (shipping_country or "").strip().lower() in INDIA_ADDRESS_NAMES


def is_au_address(shipping_country: Optional[str]) -> bool:
    return (shipping_country or "").strip().lower() in AUSTRALIA_ADDRESS_NAMES


def is_nz_address(shipping_country: Optional[str]) -> bool:
    return (shipping_country or "").strip().lower() in NEW_ZEALAND_ADDRESS_NAMES
