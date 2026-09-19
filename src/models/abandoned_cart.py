"""Abandoned-cart / guest-lead records.

Captured from the checkout form the moment a structurally valid email or
phone has been typed — NOT from anonymous cart-add browsing, which would
produce a pile of unreachable rows. Once we have a way to reach someone,
though, we snapshot everything useful about that moment (cart, pricing,
partial address, region) so the record is worth something later.

The wire model (`AbandonedCartRecordInput`) is camelCase, mirroring
`OrderCreateInput` in src/models/order.py — it comes straight off the same
checkout form. The stored document is snake_case like every other
collection. Every field on the input is optional: this fires mid-typing,
so a half-filled address is the normal case, not an error.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from bson import ObjectId
from pydantic import BaseModel, Field
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


AbandonedCartStatus = Literal["active", "converted", "expired"]


class AbandonedCartItem(BaseModel):
    productId: str
    productName: str = ""
    size: Optional[str] = None
    quantity: int = Field(default=1, ge=1)
    price: float = 0.0
    total: float = 0.0
    currency: Optional[str] = None
    sym: Optional[str] = None


class AbandonedCartPricing(BaseModel):
    subtotal: float = 0.0
    discount: float = 0.0
    shipping: float = 0.0
    total: float = 0.0


class AbandonedCartAddressSnapshot(BaseModel):
    """Every field optional — this fires while the customer is still
    typing, so a partially filled address is the expected shape."""

    fullName: Optional[str] = None
    addressLine1: Optional[str] = None
    addressLine2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postalCode: Optional[str] = None
    country: Optional[str] = None


class AbandonedCartRecordInput(BaseModel):
    """Public POST /api/cart-activity body."""

    email: Optional[str] = None
    phone: Optional[str] = None
    cartItems: List[AbandonedCartItem] = Field(default_factory=list)
    pricing: Optional[AbandonedCartPricing] = None
    shippingAddress: Optional[AbandonedCartAddressSnapshot] = None
    couponCode: Optional[str] = None
    selectedCountry: Optional[str] = None
    sessionId: Optional[str] = None
    source: Optional[str] = None


class AbandonedCart(BaseModel):
    """Stored `abandoned_carts` document."""

    id: Optional[PyObjectId] = Field(default_factory=PyObjectId, alias="_id")
    # Dedup keys. Sparse-indexed but deliberately NOT unique — a legitimate
    # merge (email matches doc A, phone matches doc B) has to be able to
    # write both identifiers onto the surviving doc, which a unique index
    # would reject mid-merge. See AbandonedCartService.record().
    email_normalized: Optional[str] = None
    phone_normalized: Optional[str] = None
    # Last-seen as typed — what an admin should actually see/contact.
    email_raw: Optional[str] = None
    phone_raw: Optional[str] = None
    cart_items: List[Dict[str, Any]] = Field(default_factory=list)
    # Denormalized so the admin list can sort/filter without unwinding.
    cart_item_count: int = 0
    cart_value: float = 0.0
    pricing: Optional[Dict[str, Any]] = None
    shipping_address: Dict[str, Any] = Field(default_factory=dict)
    coupon_code: Optional[str] = None
    selected_country: Optional[str] = None
    region: str = "default"
    currency: Optional[str] = None
    currency_symbol: Optional[str] = None
    session_id: Optional[str] = None
    source: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    status: AbandonedCartStatus = "active"
    capture_count: int = 1
    first_captured_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
    converted_at: Optional[datetime] = None
    converted_order_id: Optional[str] = None

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str, datetime: lambda v: v.isoformat()},
    }
