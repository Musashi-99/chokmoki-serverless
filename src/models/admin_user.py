from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field


class AdminUserCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=200)
    role: str
    scopes: List[str] = Field(default_factory=list)
    region: Optional[str] = None


class AdminUserDoc(BaseModel):
    """Mirrors the `admin_users` Mongo document shape."""

    id: str = Field(alias="_id")
    email: str
    name: str
    role: str
    scopes: List[str] = Field(default_factory=list)
    region: Optional[str] = None
    status: str  # "invited" | "active" | "deactivated"
    is_root: bool = False
    password_hash: Optional[str] = None
    totp_secret_encrypted: Optional[str] = None
    totp_enrolled_at: Optional[datetime] = None
    telegram_chat_id: Optional[str] = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None

    model_config = {"populate_by_name": True}


class AdminUserPublic(BaseModel):
    """Safe response shape — never includes totp_secret_encrypted or
    password_hash."""

    id: str
    email: str
    name: str
    role: str
    scopes: List[str]
    region: Optional[str] = None
    status: str
    is_root: bool
    totp_enrolled: bool
    telegram_chat_id: Optional[str] = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None

    @classmethod
    def from_doc(cls, doc: dict) -> "AdminUserPublic":
        return cls(
            id=str(doc["_id"]),
            email=doc["email"],
            name=doc["name"],
            role=doc["role"],
            scopes=doc.get("scopes") or [],
            region=doc.get("region"),
            status=doc["status"],
            is_root=bool(doc.get("is_root", False)),
            totp_enrolled=bool(doc.get("totp_enrolled_at")),
            telegram_chat_id=doc.get("telegram_chat_id"),
            created_by=doc["created_by"],
            created_at=doc["created_at"],
            updated_at=doc["updated_at"],
            last_login_at=doc.get("last_login_at"),
        )


class AdminUserSessionInfo(BaseModel):
    session_id: str
    created_at: Optional[str] = None
    last_rotated_at: Optional[str] = None
