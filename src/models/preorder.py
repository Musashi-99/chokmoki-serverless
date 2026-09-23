"""Pre-order records.

Captured from the public pre-order form on a product page when the
storefront region is pre-order-only (currently AU, but never hardcoded —
any region returned by src/models/region.py's market_codes()/
available_regions() is valid here; new regions need no code change). The
wire model (`PreorderCreateInput`) is camelCase, mirroring
`AbandonedCartRecordInput`/`OrderCreateInput` — it comes straight off the
storefront form. The stored document is snake_case like every other
collection, with plain `email`/`phone` field names (not
`contact_email`/`contactPhone`) to match `abandoned_cart.py`'s convention.

`product_name`/`product_slug` are denormalized snapshots taken at
submission time — they must survive a later product rename/deletion so an
admin reviewing an old pre-order still knows what it was for.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from bson import ObjectId
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_core import core_schema


class PyObjectId(ObjectId):
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        return core_schema.no_info_plain_validator_function(cls.validate)

    @classmethod
    def validate(cls, v):
        if isinstance(v, ObjectId):
            return v
        if isinstance(v, str):
            if not ObjectId.is_valid(v):
                raise ValueError("Invalid objectid")
            return ObjectId(v)
        raise ValueError("Invalid objectid")

    @classmethod
    def __get_pydantic_json_schema__(cls, field_schema, handler):
        field_schema.update(type="string")
        return field_schema


PreorderStatus = Literal["new", "notified", "converted", "archived"]
PREORDER_STATUSES: frozenset = frozenset({"new", "notified", "converted", "archived"})

NOTIFY_CHANNELS = frozenset({"email", "sms"})
MIN_QUANTITY = 1
MAX_QUANTITY = 10  # same cap as the storefront PDP's quantity stepper — enforced again here


class PreorderCreateInput(BaseModel):
    """Public POST /api/preorders body. `region` is deliberately NOT part of
    this model — it is resolved server-side (same resolve_country() chain
    checkout uses) from `selectedCountry` + the request's GeoIP, so a
    client can never write an arbitrary region string directly."""

    productId: str
    name: str
    email: str
    phone: Optional[str] = None
    quantity: int = 1
    size: Optional[str] = None
    notifyVia: List[str] = Field(default_factory=list)
    message: Optional[str] = ""
    selectedCountry: Optional[str] = None

    @field_validator("name", "email")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("quantity")
    @classmethod
    def _quantity_in_range(cls, v: int) -> int:
        if v < MIN_QUANTITY or v > MAX_QUANTITY:
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
        # de-dup, order-preserving
        seen: List[str] = []
        for c in cleaned:
            if c not in seen:
                seen.append(c)
        return seen

    @model_validator(mode="after")
    def _phone_required_for_sms(self) -> "PreorderCreateInput":
        if "sms" in self.notifyVia and not (self.phone or "").strip():
            raise ValueError("phone is required when notifyVia includes 'sms'")
        return self


class Preorder(BaseModel):
    """Stored `preorders` document."""

    id: Optional[PyObjectId] = Field(default_factory=PyObjectId, alias="_id")
    product_id: str
    product_name: str = ""
    product_slug: str = ""
    region: str = "default"
    name: str
    email: str
    email_normalized: str
    phone: Optional[str] = None
    quantity: int = 1
    size: Optional[str] = None
    notify_via: List[str] = Field(default_factory=list)
    message: str = ""
    status: PreorderStatus = "new"
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str, datetime: lambda v: v.isoformat()},
    }
