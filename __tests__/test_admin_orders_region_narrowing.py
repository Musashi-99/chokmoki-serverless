"""Regression test for a real usability bug: a regional admin covering
multiple regions (e.g. IN+AU) selecting a single region in the Orders page
filter still got back orders from ALL their regions, because
_region_filter() unconditionally forced the FULL region set and the route
then nulled out the client's `country` param entirely — so the dropdown
selection was silently ignored. The security boundary (can't WIDEN beyond
your own regions) was already correct; only narrowing within it was
broken. Pure unit test against _region_filter — no app/DB needed."""
from __future__ import annotations

from src.models.admin_auth import AdminPrincipal
from api.routes.admin_orders import _region_filter


def _principal(**overrides) -> AdminPrincipal:
    defaults = dict(
        email="admin@example.com",
        role="regional_admin",
        session_id="sid",
        jti="jti",
        scopes=frozenset({"orders:read"}),
        regions=frozenset(),
        is_root=False,
    )
    defaults.update(overrides)
    return AdminPrincipal(**defaults)


def test_no_requested_country_returns_full_region_set():
    principal = _principal(regions=frozenset({"IN", "AU"}))
    assert sorted(_region_filter(principal, None)) == ["AU", "IN"]


def test_requesting_one_of_own_regions_narrows_to_just_that_region():
    principal = _principal(regions=frozenset({"IN", "AU"}))
    assert _region_filter(principal, "AU") == ["AU"]
    assert _region_filter(principal, "IN") == ["IN"]


def test_requesting_a_region_outside_scope_falls_back_to_full_set_not_widened():
    principal = _principal(regions=frozenset({"IN", "AU"}))
    assert sorted(_region_filter(principal, "NZ")) == ["AU", "IN"]


def test_single_region_admin_requesting_own_region_narrows_to_itself():
    principal = _principal(regions=frozenset({"AU"}))
    assert _region_filter(principal, "AU") == ["AU"]


def test_root_is_never_filtered_regardless_of_requested_country():
    principal = _principal(is_root=True, regions=frozenset(), scopes=frozenset({"*"}))
    assert _region_filter(principal, "AU") is None
    assert _region_filter(principal, None) is None


def test_admin_with_no_regions_assigned_is_never_filtered():
    principal = _principal(regions=frozenset())
    assert _region_filter(principal, "AU") is None
