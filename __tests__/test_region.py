"""src/models/region.py — the single source of truth for admin region
codes, and the order-region enforcement helpers in
api/routes/admin_orders.py."""
from src.models.region import (
    available_regions,
    is_valid_region,
    is_valid_region_list,
    normalize_region_code,
    normalize_region_codes,
)


def test_available_regions_includes_configured_markets_and_default():
    codes = {r["code"] for r in available_regions()}
    # Matches the default SUPPORTED_MARKET_COUNTRIES=IN,AU,NZ (src/config.py)
    # used by __tests__/conftest.py's development env.
    assert {"IN", "AU", "NZ", "default"} <= codes


def test_normalize_region_code_upcases_and_trims():
    assert normalize_region_code(" in ") == "IN"
    assert normalize_region_code("au") == "AU"


def test_normalize_region_code_folds_default_casing():
    assert normalize_region_code("Default") == "default"
    assert normalize_region_code("DEFAULT") == "default"


def test_normalize_region_code_none_and_empty_stay_none():
    assert normalize_region_code(None) is None
    assert normalize_region_code("") is None
    assert normalize_region_code("   ") is None


def test_is_valid_region_accepts_known_and_default():
    assert is_valid_region("IN") is True
    assert is_valid_region("nz") is True
    assert is_valid_region("default") is True
    assert is_valid_region(None) is True  # no region = global, always valid


def test_is_valid_region_rejects_unknown_code():
    assert is_valid_region("XX") is False
    assert is_valid_region("US") is False


def test_normalize_region_codes_dedupes_and_normalizes():
    assert normalize_region_codes(["in", " AU ", "in", "Default"]) == ["IN", "AU", "default"]
    assert normalize_region_codes(None) == []
    assert normalize_region_codes([]) == []


def test_is_valid_region_list():
    assert is_valid_region_list(["IN", "AU"]) is True
    assert is_valid_region_list([]) is True
    assert is_valid_region_list(None) is True
    assert is_valid_region_list(["IN", "XX"]) is False


def test_order_region_enforcement_helpers():
    from api.routes.admin_orders import _enforce_order_region, _order_region, _region_filter
    from fastapi import HTTPException
    from src.models.admin_auth import AdminPrincipal

    def principal(**overrides):
        defaults = dict(
            email="a@b.com", role="regional_admin", session_id="s", jti="j",
            scopes=frozenset({"orders:read"}), regions=frozenset(), is_root=False,
        )
        defaults.update(overrides)
        return AdminPrincipal(**defaults)

    order_in = {"region_audit": {"pricing_country_used": "IN"}}
    order_au = {"region_audit": {"pricing_country_used": "AU"}}
    order_nz = {"region_audit": {"pricing_country_used": "NZ"}}
    order_default = {"region_audit": {"pricing_country_used": "default"}}
    order_none = {}

    assert _order_region(order_in) == "IN"
    assert _order_region(order_default) == "default"
    assert _order_region(order_none) == "default"

    # No regions on the principal (global admin) -> never restricted.
    global_admin = principal(regions=frozenset())
    _enforce_order_region(global_admin, order_au)  # must not raise
    assert _region_filter(global_admin) is None

    # Root is never restricted even if regions happen to be set.
    root = principal(regions=frozenset({"IN"}), is_root=True)
    _enforce_order_region(root, order_au)  # must not raise
    assert _region_filter(root) is None

    # Regional admin scoped to IN can see IN orders...
    in_admin = principal(regions=frozenset({"IN"}))
    _enforce_order_region(in_admin, order_in)  # must not raise
    assert _region_filter(in_admin) == ["IN"]

    # ...but not AU orders.
    try:
        _enforce_order_region(in_admin, order_au)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404

    # An admin covering BOTH IN and AU can see either, but not a third region.
    multi_admin = principal(regions=frozenset({"IN", "AU"}))
    _enforce_order_region(multi_admin, order_in)  # must not raise
    _enforce_order_region(multi_admin, order_au)  # must not raise
    assert set(_region_filter(multi_admin)) == {"IN", "AU"}
    try:
        _enforce_order_region(multi_admin, order_nz)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404

    # Case-insensitive / "default" folding still enforced correctly.
    default_admin = principal(regions=frozenset({"Default"}))
    _enforce_order_region(default_admin, order_default)  # must not raise
    try:
        _enforce_order_region(default_admin, order_in)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404
