from dataclasses import dataclass, field
from typing import FrozenSet, Optional


@dataclass(frozen=True)
class AdminPrincipal:
    email: str
    role: str
    session_id: str
    jti: str
    scopes: FrozenSet[str] = field(default_factory=frozenset)
    region: Optional[str] = None
    is_root: bool = False

    def has_permission(self, permission: str) -> bool:
        """Legacy role-default check, kept for any caller not yet migrated
        to Vakt (src/security/abac.py). New route protection should use
        require_scope, which checks `scopes` directly."""
        from src.models.admin_rbac import role_has_permission

        return role_has_permission(self.role, permission)


@dataclass(frozen=True)
class AuthTokens:
    access_token: str
    refresh_token: str
    csrf_token: str
    session_id: str
    expires_in: int
    token_type: str = "Bearer"


@dataclass(frozen=True)
class LoginResult:
    email: str
    role: str
    tokens: AuthTokens
    mfa_required: bool = False
    scopes: FrozenSet[str] = field(default_factory=frozenset)
    region: Optional[str] = None
    is_root: bool = False
