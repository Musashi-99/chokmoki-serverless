"""Single source of truth for admin-facing "region" — deliberately the same
taxonomy already used for order pricing (src/pricing/geo_provider.py's
`supported_countries()`/`map_to_market()` and Order.region_audit.
pricing_country_used): each code in `settings.supported_market_countries`
plus a "default" bucket for everything else.

Adding a new region later is a one-line env var change
(SUPPORTED_MARKET_COUNTRIES) — no code change required. A human-readable
label is optional (falls back to the bare code) via REGION_LABELS below.
"""
from __future__ import annotations

from typing import List, Optional

from src.config import settings

# Optional nicer display names — a region works correctly without an entry
# here (falls back to its code), so this is purely cosmetic and never blocks
# adding a new market.
REGION_LABELS: dict[str, str] = {
    "IN": "India",
    "AU": "Australia",
    "NZ": "New Zealand",
}
DEFAULT_REGION_CODE = "default"
DEFAULT_REGION_LABEL = "Rest of World"


def market_codes() -> list[str]:
    return [
        c.strip().upper()
        for c in (settings.supported_market_countries or "").split(",")
        if c.strip()
    ]


def available_regions() -> list[dict]:
    """Ordered list of {code, label} — the exact set an admin's `region`
    may be set to, and the exact set the invite form's select renders."""
    regions = [{"code": code, "label": REGION_LABELS.get(code, code)} for code in market_codes()]
    regions.append({"code": DEFAULT_REGION_CODE, "label": DEFAULT_REGION_LABEL})
    return regions


def normalize_region_code(code: Optional[str]) -> Optional[str]:
    """None/empty stays None (global, no region scoping). Otherwise
    upper-cases known market codes and folds any casing of "default" to the
    canonical "default" — mirrors the normalization already applied to the
    `country` query param in src/services/order_service.py's list/count."""
    if not code:
        return None
    trimmed = code.strip()
    if not trimmed:
        return None
    return DEFAULT_REGION_CODE if trimmed.lower() == DEFAULT_REGION_CODE else trimmed.upper()


def is_valid_region(code: Optional[str]) -> bool:
    normalized = normalize_region_code(code)
    if normalized is None:
        return True
    return normalized in {r["code"] for r in available_regions()}


def normalize_region_codes(codes: Optional[List[str]]) -> List[str]:
    """A cleaned, de-duplicated, order-preserving list of region codes — the
    plural counterpart to normalize_region_code(), for an admin who may be
    assigned more than one region (e.g. one admin covering both IN and AU)."""
    if not codes:
        return []
    seen: list[str] = []
    for code in codes:
        normalized = normalize_region_code(code)
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def is_valid_region_list(codes: Optional[List[str]]) -> bool:
    return all(is_valid_region(c) for c in normalize_region_codes(codes)) if codes else True
