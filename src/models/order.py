from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field
from bson import ObjectId
from datetime import datetime
from src.models.product import PyObjectId
from src.models.coupon import AppliedDiscount
from src.models.shipping_address import ShippingAddress as ShippingAddressModel


class ShippingAddress(BaseModel):
    _id: Optional[str] = None
    email: str
    full_name: str
    phone: str
    address_line1: str
    address_line2: Optional[str] = ""
    city: str
    state: Optional[str] = ""
    postal_code: str
    country: str
    is_default: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class OrderItemInput(BaseModel):
    productId: str
    productName: str
    variant: Dict[str, str]  # Keep as dict for variant flexibility
    quantity: int = Field(ge=1)
    price: float
    total: float
    size: Optional[str] = None


class OrderPricingInput(BaseModel):
    subtotal: float
    discount: float
    shipping: float
    total: float


class OrderCreateInput(BaseModel):
    shippingAddress: ShippingAddress
    items: List[OrderItemInput]
    specialMessage: Optional[str] = ""
    pricing: OrderPricingInput
    userEmail: str
    timestamp: str
    paymentMethod: Optional[Literal["razorpay", "cod"]] = "cod"
    couponCode: Optional[str] = None
    # The pricing market the storefront had active at checkout (explicit
    # selection, or the first-visit GeoIP guess if the customer never
    # changed it) — "IN" | "AU" | "NZ" | "default". Used to price the order
    # and, together with the server-derived IP country, to build the
    # region audit trail / fraud mismatch check. Never trusted for pricing
    # math itself beyond picking which MarketPrice bucket to use.
    selectedCountry: Optional[str] = None
    # Set server-side only (api/routes/orders.py resolves it from the
    # customer auth cookie, if present) — never trust a client-supplied
    # value here, or anyone could claim someone else's account on an order.
    user_id: Optional[str] = None
    # Meta ad-attribution signals from the browser (the _fbp/_fbc cookies and
    # the page the order was placed from) — stored on the order so the
    # server-side Purchase can carry them, even when it is sent later from a
    # payment webhook that has no browser context.
    fbp: Optional[str] = Field(default=None, max_length=256)
    fbc: Optional[str] = Field(default=None, max_length=256)
    eventSourceUrl: Optional[str] = Field(default=None, max_length=512)


class ValidatedOrderItem(BaseModel):
    product_id: str
    product_name: str
    variant: Dict[str, str]  # Keep as dict for variant flexibility
    quantity: int = Field(ge=1)
    unit_price: float
    total_price: float
    size: Optional[str] = None
    # Which MarketPrice bucket this item was actually priced in — resolved
    # once per order (see OrderService._validate_and_prepare_order) and
    # carried all the way to the Order document, so nothing downstream
    # (ThankYou page, invoices) has to guess the currency from a country
    # code after the fact. Defaults match the legacy price_inr path.
    currency: str = "INR"
    sym: str = "₹"


class OrderStatusExtras(BaseModel):
    """DTO for order status extras"""
    pass  # Will be populated dynamically


class OrderStatus(BaseModel):
    type: Literal[
        "accepted",
        "rejected",
        "rejected_by_user",
        "delivered",
        "out_for_delivery",
        "agent",
        "agent_changed",
        "in_hub",
        "cancelled",
        "cancellation_requested",
        "refunded",
        "refund_requested",
    ]
    reason: str = ""
    extras: Dict[str, Any] = {}  # Keep flexible for dynamic data


class ShippingAddressInOrder(BaseModel):
    """DTO for shipping address within order"""
    _id: Optional[str] = None
    email: str
    full_name: str
    phone: str
    address_line1: str
    address_line2: Optional[str] = ""
    city: str
    state: str
    postal_code: str
    country: str
    is_default: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class RegionAudit(BaseModel):
    """Full evidence trail for the region/pricing decision made on this
    order — kept as verbose as possible so an admin reviewing a flagged
    order never has to go dig through logs."""
    ip: Optional[str] = None
    ip_country: Optional[str] = None                       # GeoIP-resolved bucket (IN/AU/NZ/default)
    ip_country_raw: Optional[str] = None                   # raw ISO code from the provider, pre-mapping
    selected_country: Optional[str] = None                 # from the client at checkout
    detected_country_at_bootstrap: Optional[str] = None    # first-visit GeoIP guess, if still known client-side
    pricing_country_used: str                              # which MarketPrice bucket was actually applied
    country_mismatch: bool = False
    user_agent: Optional[str] = None
    geoip_raw_response: Optional[Dict[str, Any]] = None    # full adapter payload, evidence for admin review
    resolved_at: datetime = Field(default_factory=datetime.utcnow)


class Order(BaseModel):
    id: Optional[PyObjectId] = Field(default_factory=PyObjectId, alias="_id")
    order_id: str
    # Strongest link to an account — set when the customer was logged in
    # (verified session) at checkout. None for guest orders, which only
    # match by email/phone.
    user_id: Optional[str] = None
    user_email: str
    shipping_address: ShippingAddressInOrder  # Changed from Dict to DTO
    items: List[ValidatedOrderItem]
    special_message: Optional[str] = ""
    subtotal: float
    discount: float
    shipping: float
    total_amount: float
    # The currency every amount on this order (subtotal/discount/shipping/
    # total_amount, and each item's unit_price/total_price) is actually
    # denominated in — resolved once at order-creation time from the
    # MarketPrice bucket used (see ValidatedOrderItem.currency). Never
    # inferred after the fact from region_audit, so a customer's confirmation
    # page and any invoice always show the real currency they were charged.
    currency: str = "INR"
    currency_symbol: str = "₹"
    applied_discount: Optional[AppliedDiscount] = None
    payment_method: Optional[Literal["razorpay", "cod"]] = "cod"
    payment_status: Optional[Literal["pending", "completed", "failed"]] = None
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    status: OrderStatus = Field(default_factory=lambda: OrderStatus(type="accepted"))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    raw_order_log: Dict[str, Any]  # Keep as dict for raw log flexibility
    region_audit: Optional[RegionAudit] = None

    # --- Warehouse fulfillment + Shiprocket shipping (independent of
    # payment_status and status.type — see src/services/shiprocket_service.py)
    fulfillment_status: Literal["pending", "packed", "shipped"] = "pending"
    shiprocket_order_id: Optional[int] = None
    shiprocket_shipment_id: Optional[int] = None
    awb_code: Optional[str] = None
    courier_name: Optional[str] = None
    courier_company_id: Optional[int] = None
    tracking_url: Optional[str] = None
    shipping_label_url: Optional[str] = None
    shipping_invoice_url: Optional[str] = None
    shipment_status: Literal[
        "pending",
        "awb_assigned",
        "pickup_scheduled",
        "picked_up",
        "in_transit",
        "out_for_delivery",
        "delivered",
        "rto_initiated",
        "rto_delivered",
        "cancellation_requested",
        "cancelled",
        "failed",
    ] = "pending"
    shipment_status_history: List[Dict[str, Any]] = Field(default_factory=list)
    shipment_scans: List[Dict[str, Any]] = Field(default_factory=list)

    # --- Unified order-status system (admin-only annotations, independent of
    # any system-driven status field above) ---
    custom_status: Optional[Literal[
        "on_hold",
        "priority",
        "escalated",
        "awaiting_customer_response",
        "damaged",
        "needs_review",
        "refund_requested",
        "refunded",
        "duplicate",
    ]] = None
    notes: List[Dict[str, Any]] = Field(default_factory=list)  # [{text, author_email, created_at}], append-only

    # Tracks whether complete_pending_order()'s post-insert side effects
    # (inventory commit, ledger, alert, fraud-mark, reconcile-resolve) fully
    # finished — "pending"/"in_progress" means a crash interrupted them and
    # the next call will retry just those, not the whole order. Absent/None
    # on COD orders (created via create()/create_from_admin(), a different
    # code path). post_processing_claimed_at supports reclaiming a stale
    # "in_progress" claim if the original claimer itself crashed.
    post_processing_status: Optional[Literal["pending", "in_progress", "done"]] = None
    post_processing_claimed_at: Optional[datetime] = None

    # Assigned exactly once, on first invoice-PDF generation (atomic counter,
    # src/services/invoice_service.py) — GST invoice numbers must be unique,
    # sequential, and immutable once issued.
    invoice_number: Optional[str] = None
    invoice_date: Optional[datetime] = None

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str, datetime: lambda v: v.isoformat()}
    }

