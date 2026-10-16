"""General enquiries — captured from the public /enquiry page.

Primarily used in person: the shop admin opens the page on their laptop and
visitors type their own details, so a product is optional. Quantity and size
only mean something alongside a product and are dropped without one.
Otherwise the shape follows pre-orders (name + email required, phone
optional, notify-via channels, message). The wire model is camelCase like
`PreorderCreateInput`; the stored document is snake_case.

`product_name`/`product_slug` are snapshots taken at submission so the row
still makes sense after a product is renamed or deleted."""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from src.models.preorder import MAX_QUANTITY, MIN_QUANTITY, NOTIFY_CHANNELS

EnquiryStatus = Literal["new", "contacted", "converted", "archived"]
ENQUIRY_STATUSES: frozenset = frozenset({"new", "contacted", "converted", "archived"})


class EnquiryCreateInput(BaseModel):
    name: str = Field(max_length=120)
    email: str = Field(max_length=254)
    phone: Optional[str] = Field(default=None, max_length=32)
    productId: Optional[str] = Field(default=None, max_length=64)
    quantity: Optional[int] = None
    size: Optional[str] = Field(default=None, max_length=60)
    notifyVia: List[str] = Field(default_factory=list)
    message: Optional[str] = Field(default="", max_length=2000)
    # Honeypot: real people never see or fill this field; bots often do.
    website: Optional[str] = Field(default=None, max_length=200)

    @field_validator("name", "email")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("productId", "size", "phone")
    @classmethod
    def _blank_to_none(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None

    @field_validator("quantity")
    @classmethod
    def _quantity_in_range(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and (v < MIN_QUANTITY or v > MAX_QUANTITY):
            raise ValueError(f"quantity must be between {MIN_QUANTITY} and {MAX_QUANTITY}")
        return v

    @field_validator("notifyVia")
    @classmethod
    def _notify_via_valid(cls, v: List[str]) -> List[str]:
        cleaned = [str(c).strip().lower() for c in (v or []) if str(c).strip()]
        if not cleaned:
            raise ValueError("notifyVia must include at least one channel")
        invalid = set(cleaned) - NOTIFY_CHANNELS
        if invalid:
            raise ValueError(f"notifyVia may only contain {sorted(NOTIFY_CHANNELS)}, got {sorted(invalid)}")
        seen: List[str] = []
        for c in cleaned:
            if c not in seen:
                seen.append(c)
        return seen

    @model_validator(mode="after")
    def _consistency(self) -> "EnquiryCreateInput":
        if "sms" in self.notifyVia and not self.phone:
            raise ValueError("phone is required when notifyVia includes 'sms'")
        if not self.productId:
            # Quantity/size describe a product; without one they'd be noise.
            self.quantity = None
            self.size = None
        elif self.quantity is None:
            self.quantity = MIN_QUANTITY
        return self

    @property
    def is_spam(self) -> bool:
        return bool((self.website or "").strip())


class Enquiry(BaseModel):
    """Stored `enquiries` document."""

    name: str
    email: str
    email_normalized: str
    phone: Optional[str] = None
    product_id: Optional[str] = None
    product_name: str = ""
    product_slug: str = ""
    quantity: Optional[int] = None
    size: Optional[str] = None
    notify_via: List[str] = Field(default_factory=list)
    message: str = ""
    status: EnquiryStatus = "new"
    source: str = "enquiry_page"
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
