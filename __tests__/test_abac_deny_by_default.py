"""Vakt ABAC layer (src/security/abac.py) — security-invariant unit tests
that don't need Mongo/Redis: pure policy-evaluation logic against
build_inquiry()/guard.is_allowed().
"""
from src.models.admin_auth import AdminPrincipal
from src.security.abac import is_allowed


def _principal(**overrides) -> AdminPrincipal:
    defaults = dict(
        email="admin@example.com",
        role="admin",
        session_id="sid",
        jti="jti",
        scopes=frozenset(),
        regions=frozenset(),
        is_root=False,
    )
    defaults.update(overrides)
    return AdminPrincipal(**defaults)


def test_deny_by_default_no_scopes():
    principal = _principal(scopes=frozenset())
    assert is_allowed(principal, "orders", "read") is False


def test_deny_by_default_unmapped_resource():
    principal = _principal(scopes=frozenset({"orders:read"}))
    assert is_allowed(principal, "nonexistent-resource", "read") is False


def test_allow_when_scope_matches():
    principal = _principal(scopes=frozenset({"orders:read"}))
    assert is_allowed(principal, "orders", "read") is True
    assert is_allowed(principal, "orders", "write") is False


def test_wildcard_scope_allows_everything_mapped_except_admins_without_root():
    # "*" alone is not enough for the admins resource — that gate also
    # requires is_root=True (see test_admins_resource_requires_root_even_with_scope).
    principal = _principal(scopes=frozenset({"*"}))
    assert is_allowed(principal, "orders", "read") is True
    assert is_allowed(principal, "products", "write") is True
    assert is_allowed(principal, "admins", "write") is False


def test_admins_resource_requires_root_even_with_scope():
    principal = _principal(scopes=frozenset({"admins:write"}), is_root=False)
    assert is_allowed(principal, "admins", "write") is False


def test_admins_resource_allowed_for_root_with_scope():
    principal = _principal(scopes=frozenset({"admins:write"}), is_root=True)
    assert is_allowed(principal, "admins", "write") is True


def test_root_wildcard_scope_and_is_root_allows_admins():
    principal = _principal(scopes=frozenset({"*"}), is_root=True)
    assert is_allowed(principal, "admins", "read") is True
    assert is_allowed(principal, "admins", "write") is True


def test_region_never_gates_access():
    """Region must never appear in any Vakt rule — a regional admin with
    the right scope is allowed regardless of region value, and a missing
    region never blocks an otherwise-permitted action."""
    principal_a = _principal(scopes=frozenset({"orders:read"}), regions=frozenset({"IN"}))
    principal_b = _principal(scopes=frozenset({"orders:read"}), regions=frozenset())
    assert is_allowed(principal_a, "orders", "read") is True
    assert is_allowed(principal_b, "orders", "read") is True
