from enum import Enum


class AdminRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    REGIONAL_ADMIN = "regional_admin"


class AdminPermission(str, Enum):
    ACCESS = "admin:access"
    ORDERS_READ = "orders:read"
    ORDERS_WRITE = "orders:write"
    PRODUCTS_READ = "products:read"
    PRODUCTS_WRITE = "products:write"
    PRODUCTS_PRICE_WRITE = "products:price_write"
    COUPONS_READ = "coupons:read"
    COUPONS_WRITE = "coupons:write"
    CONTENT_WRITE = "content:write"
    MEDIA_UPLOAD = "media:upload"
    SETTINGS_WRITE = "settings:write"
    INBOX_READ = "inbox:read"
    AUDIT_READ = "audit:read"
    ADMINS_READ = "admins:read"
    ADMINS_WRITE = "admins:write"


# Authoritative access for a given admin lives in that admin's own
# `admin_users.scopes` list (src/models/admin_user.py), not here — a single
# role->permission mapping can't express "this ADMIN has orders:* but not
# content:write". ROLE_PERMISSIONS below is used only as the *default* scope
# template pre-filled on the invite form (api/routes/admin_users.py); it is
# never read at authorization-decision time (see src/security/abac.py).
ROLE_PERMISSIONS: dict[str, set[str]] = {
    AdminRole.SUPER_ADMIN.value: {"*"},
    AdminRole.ADMIN.value: {
        AdminPermission.ORDERS_READ.value,
        AdminPermission.ORDERS_WRITE.value,
        AdminPermission.PRODUCTS_READ.value,
        AdminPermission.PRODUCTS_WRITE.value,
        AdminPermission.COUPONS_READ.value,
        AdminPermission.COUPONS_WRITE.value,
        AdminPermission.INBOX_READ.value,
    },
    # Regional admins are intentionally narrow: view orders in their own
    # region(s), manage coupons in their own region(s), edit product prices
    # in their own region(s) only. No content/settings/media/inbox/admin
    # access, and no full products:write (see admin_catalog.py's
    # require_any_scope(products:write, products:price_write) split).
    AdminRole.REGIONAL_ADMIN.value: {
        AdminPermission.ORDERS_READ.value,
        AdminPermission.PRODUCTS_READ.value,
        AdminPermission.PRODUCTS_PRICE_WRITE.value,
        AdminPermission.COUPONS_READ.value,
        AdminPermission.COUPONS_WRITE.value,
    },
}


def permissions_for_role(role: str) -> set[str]:
    return set(ROLE_PERMISSIONS.get(role, set()))


def role_has_permission(role: str, permission: str) -> bool:
    perms = permissions_for_role(role)
    return "*" in perms or permission in perms
