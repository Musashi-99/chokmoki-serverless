from __future__ import annotations

from typing import Any, Dict, Optional

from src.alerts.chain import AlertEvent, AlertHandler
from src.alerts.channels import NotificationChannel, SmsChannel
from src.alerts.events import (
    EVENT_ADMIN_MUTATION,
    EVENT_CONTACT_SUBMITTED,
    EVENT_NEWSLETTER_SUBSCRIBED,
    EVENT_ORDER_CREATED,
    EVENT_PRODUCT_PRICE_CHANGED,
    EVENT_SHIPMENT_UPDATE,
    EVENT_SYSTEM_ERROR,
)
from src.plugins.logger import logger
from src.services.email_service import EmailService
from src.services.email_templates import render_order_confirmation_email, render_order_status_email

# Admin-mutation resources worth a "setting changed" alert. Deliberately
# excludes: "orders" (has its own richer alert via OrderCreatedHandler),
# "products" (routine catalog edits are too frequent to be alert-worthy,
# but a price change specifically gets its own richer alert via
# PriceChangedHandler), "upload" (not a setting), "fraud" (not requested
# yet — add its own handler link later if wanted).
SETTINGS_RESOURCES = {
    "hero",
    "navigation",
    "home-page",
    "shop-page",
    "policies",
    "studio-settings",
    "contact-page",
    "account-page",
    "history-page",
    "product-page",
    "site-assets",
    "faq",
    "collection-slides",
    "testimonials",
    "categories",
    "blog-posts",
    "journal",
    "inbox",
    # Admin-user management (invite/deactivate/regenerate-TOTP/session
    # revoke) — api/routes/admin_users.py. Always alert-worthy: this is
    # exactly the "who is doing what" surface the multi-admin feature needs
    # visible in Telegram.
    "admins",
}


def _format_order_alert(payload: Dict[str, Any]) -> str:
    order_id = payload.get("order_id", "unknown")
    total = payload.get("total_amount", 0)
    total_label = f"₹{total:,.0f}" if isinstance(total, (int, float)) else f"₹{total}"
    customer = payload.get("customer_name") or payload.get("user_email", "")
    payment_method = (payload.get("payment_method") or "").upper()

    lines = [
        "🎉 *New Order Received*",
        f"Order: `{order_id}`",
        f"Customer: {customer}",
        f"Total: {total_label}",
    ]
    if payment_method:
        lines.append(f"Payment: {payment_method}")

    items = payload.get("items", [])
    if items:
        lines.append("")
        lines.append("Items:")
        for item in items[:10]:
            name = item.get("product_name", "item")
            qty = item.get("quantity", 1)
            lines.append(f"• {name} × {qty}")

    return "\n".join(lines)


# action -> readable verb, for the admin-management events fired directly
# by api/routes/admin_users.py (publish_alert with a specific `action` and
# metadata, not the generic per-path one AdminAuditMiddleware fires for
# every mutating /api/admin/* route). Lets "which admin did what" show up
# as an actual sentence instead of a raw method+path.
ADMIN_MGMT_ACTION_VERBS: Dict[str, str] = {
    "invite_admin": "invited",
    "deactivate_admin": "deactivated",
    "reactivate_admin": "reactivated",
    "regenerate_totp": "regenerated the QR/TOTP for",
    "revoke_session": "revoked a session for",
    "revoke_all_sessions": "revoked all sessions for",
}


def _format_settings_alert(payload: Dict[str, Any]) -> str:
    actor = payload.get("actor_email", "unknown admin")
    resource = payload.get("resource", "unknown")
    action = payload.get("action")

    if resource == "admins" and action in ADMIN_MGMT_ACTION_VERBS:
        verb = ADMIN_MGMT_ACTION_VERBS[action]
        target = payload.get("invited_email") or payload.get("target_email") or "an admin"
        lines = ["👤 *Admin Management*", f"{actor} {verb} `{target}`"]
        region = payload.get("region")
        if region:
            lines.append(f"Region: {region}")
        count = payload.get("count")
        if count is not None:
            lines.append(f"Sessions revoked: {count}")
        return "\n".join(lines)

    method = payload.get("method", "")
    path = payload.get("path", "")
    return f"⚙️ *Setting Updated*\n{actor} updated `{resource}`\n{method} {path}"


def _format_price_label(value: Any) -> str:
    return f"₹{value:,.0f}" if isinstance(value, (int, float)) else f"₹{value}"


def _format_price_changed_alert(payload: Dict[str, Any]) -> str:
    name = payload.get("product_name", "product")
    old_price = _format_price_label(payload.get("old_price"))
    new_price = _format_price_label(payload.get("new_price"))
    actor = payload.get("actor_email")
    lines = [
        "💰 *Price Changed*",
        name,
        f"{old_price} → {new_price}",
    ]
    if actor:
        lines.append(f"By: {actor}")
    return "\n".join(lines)


def _format_contact_alert(payload: Dict[str, Any]) -> str:
    name = payload.get("name") or "Someone"
    email = payload.get("email", "")
    message = (payload.get("message") or payload.get("note") or "").strip()
    lines = [
        "📩 *New Contact Submission*",
        f"{name} ({email})" if email else name,
    ]
    if message:
        lines.append("")
        lines.append(message[:500])
    return "\n".join(lines)


def _format_newsletter_alert(payload: Dict[str, Any]) -> str:
    email = payload.get("email", "unknown")
    source = payload.get("source", "")
    lines = ["📰 *New Newsletter Signup*", email]
    if source:
        lines.append(f"Source: {source}")
    return "\n".join(lines)


def _format_shipment_alert(payload: Dict[str, Any]) -> str:
    order_id = payload.get("order_id", "unknown")
    status = (payload.get("status") or "unknown").replace("_", " ").title()
    lines = [
        "📦 *Shipment Update*",
        f"Order: `{order_id}`",
        f"Status: {status}",
    ]
    if payload.get("courier_name"):
        lines.append(f"Courier: {payload['courier_name']}")
    if payload.get("awb_code"):
        lines.append(f"AWB: {payload['awb_code']}")
    return "\n".join(lines)


async def _send_order_email(order_id: str, kind: str, status: Optional[str] = None) -> None:
    """Best-effort, independent of SMS/Telegram outcome — mirrors
    _send_lifecycle_sms's never-raise posture. Fetches the full Order (the
    alert payload only carries a lightweight summary) since the email
    template needs shipping address + per-item pricing.
    """
    try:
        from src.services.order_service import OrderService

        order = await OrderService().get_by_id(order_id)
        if not order or not order.user_email:
            return

        if kind == "confirmation":
            subject, html = render_order_confirmation_email(order)
        else:
            rendered = render_order_status_email(order, status or "")
            if not rendered:
                return
            subject, html = rendered

        await EmailService().send(order.user_email, subject, html)
    except Exception as e:
        if logger:
            logger.error(f"Order email '{kind}' failed for order {order_id}: {e}")


async def _resolve_actor_chat_ids(actor_email: Optional[str]) -> list[str]:
    """For alerts that carry an acting admin's email, resolve where to also
    mirror the notification: the admin's own telegram_chat_id if set, else
    their region's designated REGIONAL_ADMIN's chat id. Returns [] (never
    raises) if admin_users lookups aren't available — this must never block
    the global send."""
    if not actor_email:
        return []
    try:
        from src.services.admin_user_service import AdminUserService

        admin_users = AdminUserService()
        actor = await admin_users.get_routing_for_actor(actor_email)
        if not actor:
            return []
        if actor.get("telegram_chat_id"):
            return [actor["telegram_chat_id"]]
        region = actor.get("region")
        if region:
            region_chat_id = await admin_users.get_region_chat_id(region)
            if region_chat_id:
                return [region_chat_id]
        return []
    except Exception as exc:
        if logger:
            logger.warning(f"Telegram routing lookup failed for {actor_email}: {exc}")
        return []


async def _send_to_channel(
    channel: NotificationChannel,
    text: str,
    label: str,
    *,
    actor_email: Optional[str] = None,
) -> None:
    """Every handler below only has ops-facing side effects left to do here
    (customer-facing SMS/email already happened, independently, above) — a
    disabled Telegram channel is a deliberate config choice, not a failure,
    so it must NOT raise (that would mark the whole stream event as failed
    and trigger a redelivery, which would re-run everything above it too —
    including re-sending the customer's order-confirmation/status email on
    every retry, for as long as Telegram stays off). Only an ACTUALLY
    enabled channel failing to send is worth raising/retrying over.

    `actor_email`, when the event payload carries one (admin mutations,
    price changes), is resolved to the acting admin's personal/region
    Telegram chat so root's global chat AND the acting admin's own chat both
    see it — "know which user is doing what".
    """
    if not channel.is_enabled():
        return
    extra_chat_ids = await _resolve_actor_chat_ids(actor_email)
    sent = await channel.send(text, extra_chat_ids=extra_chat_ids)
    if not sent:
        raise RuntimeError(f"{label} channel send failed")


async def _send_lifecycle_sms(
    sms_channel: Optional[SmsChannel], template_key: str, payload: Dict[str, Any]
) -> None:
    """Best-effort, independent of the Telegram channel's outcome — a
    failed/skipped SMS must never raise (that would trigger the stream
    consumer's Telegram-oriented retry logic for an unrelated channel).
    """
    if not sms_channel or not sms_channel.is_enabled():
        return
    phone = payload.get("customer_phone") or ""
    if not phone:
        if logger:
            logger.info(f"Lifecycle SMS '{template_key}' skipped: no customer phone on payload")
        return
    try:
        await sms_channel.send(
            phone,
            template_key,
            {
                "order_id": payload.get("order_id", ""),
                "customer_name": payload.get("customer_name", ""),
            },
        )
    except Exception as e:
        if logger:
            logger.error(f"Lifecycle SMS '{template_key}' failed for order {payload.get('order_id')}: {e}")


class OrderCreatedHandler(AlertHandler):
    def __init__(self, channel: NotificationChannel, sms_channel: Optional[SmsChannel] = None) -> None:
        super().__init__()
        self._channel = channel
        self._sms_channel = sms_channel

    async def _can_handle(self, event: AlertEvent) -> bool:
        return event.type == EVENT_ORDER_CREATED

    async def _process(self, event: AlertEvent) -> bool:
        await _send_lifecycle_sms(self._sms_channel, "order_placed", event.payload)
        order_id = event.payload.get("order_id")
        if order_id:
            await _send_order_email(order_id, "confirmation")
        await _send_to_channel(self._channel, _format_order_alert(event.payload), "Order alert")
        return True


class SettingsChangedHandler(AlertHandler):
    def __init__(self, channel: NotificationChannel) -> None:
        super().__init__()
        self._channel = channel

    async def _can_handle(self, event: AlertEvent) -> bool:
        return event.type == EVENT_ADMIN_MUTATION and event.payload.get("resource") in SETTINGS_RESOURCES

    async def _process(self, event: AlertEvent) -> bool:
        await _send_to_channel(
            self._channel,
            _format_settings_alert(event.payload),
            "Settings alert",
            actor_email=event.payload.get("actor_email"),
        )
        return True


class PriceChangedHandler(AlertHandler):
    def __init__(self, channel: NotificationChannel) -> None:
        super().__init__()
        self._channel = channel

    async def _can_handle(self, event: AlertEvent) -> bool:
        return event.type == EVENT_PRODUCT_PRICE_CHANGED

    async def _process(self, event: AlertEvent) -> bool:
        await _send_to_channel(
            self._channel,
            _format_price_changed_alert(event.payload),
            "Price-change alert",
            actor_email=event.payload.get("actor_email"),
        )
        return True


class ContactSubmittedHandler(AlertHandler):
    def __init__(self, channel: NotificationChannel) -> None:
        super().__init__()
        self._channel = channel

    async def _can_handle(self, event: AlertEvent) -> bool:
        return event.type == EVENT_CONTACT_SUBMITTED

    async def _process(self, event: AlertEvent) -> bool:
        await _send_to_channel(self._channel, _format_contact_alert(event.payload), "Contact alert")
        return True


class NewsletterSubscribedHandler(AlertHandler):
    def __init__(self, channel: NotificationChannel) -> None:
        super().__init__()
        self._channel = channel

    async def _can_handle(self, event: AlertEvent) -> bool:
        return event.type == EVENT_NEWSLETTER_SUBSCRIBED

    async def _process(self, event: AlertEvent) -> bool:
        await _send_to_channel(self._channel, _format_newsletter_alert(event.payload), "Newsletter alert")
        return True


# shipment_status (src/models/order.py) -> sms_templates key. Statuses not
# listed here (pending/awb_assigned/pickup_scheduled/in_transit/
# rto_delivered/cancellation_requested/failed) don't have a distinct
# customer-facing SMS — intermediate/internal states, not worth a text.
SHIPMENT_STATUS_SMS_KEY = {
    "picked_up": "order_shipped",
    "out_for_delivery": "order_out_for_delivery",
    "delivered": "order_delivered",
    "cancelled": "order_cancelled",
    "rto_initiated": "order_cancelled",
}


class ShipmentUpdateHandler(AlertHandler):
    def __init__(self, channel: NotificationChannel, sms_channel: Optional[SmsChannel] = None) -> None:
        super().__init__()
        self._channel = channel
        self._sms_channel = sms_channel

    async def _can_handle(self, event: AlertEvent) -> bool:
        return event.type == EVENT_SHIPMENT_UPDATE

    async def _process(self, event: AlertEvent) -> bool:
        status = event.payload.get("status")
        sms_key = SHIPMENT_STATUS_SMS_KEY.get(status)
        if sms_key:
            await _send_lifecycle_sms(self._sms_channel, sms_key, event.payload)
        order_id = event.payload.get("order_id")
        if order_id and status:
            await _send_order_email(order_id, "status", status)
        await _send_to_channel(self._channel, _format_shipment_alert(event.payload), "Shipment alert")
        return True


def _format_system_error_alert(payload: Dict[str, Any]) -> str:
    component = payload.get("component", "unknown")
    message = payload.get("message", "")
    lines = [
        "🚨 *System Error*",
        f"Component: `{component}`",
        message,
    ]
    context = payload.get("context") or {}
    if context:
        # Keep it short — this is a notification, not the full audit record
        # (that's what the admin panel's System Logs view is for).
        preview = ", ".join(f"{k}={v}" for k, v in list(context.items())[:5])
        lines.append(f"_{preview}_")
    return "\n".join(lines)


class SystemErrorHandler(AlertHandler):
    def __init__(self, channel: NotificationChannel) -> None:
        super().__init__()
        self._channel = channel

    async def _can_handle(self, event: AlertEvent) -> bool:
        return event.type == EVENT_SYSTEM_ERROR

    async def _process(self, event: AlertEvent) -> bool:
        await _send_to_channel(self._channel, _format_system_error_alert(event.payload), "System error alert")
        return True


class FallbackHandler(AlertHandler):
    """End of the chain — always matches. Logs and drops anything nobody
    else claimed (unknown event types, or admin mutations on resources
    intentionally excluded from SETTINGS_RESOURCES) so nothing is ever lost
    or crashes the consumer.
    """

    async def _can_handle(self, event: AlertEvent) -> bool:
        return True

    async def _process(self, event: AlertEvent) -> bool:
        if logger:
            logger.info(f"Alert event '{event.type}' had no interested handler; dropped")
        return True


def build_chain(channel: NotificationChannel, sms_channel: Optional[SmsChannel] = None) -> AlertHandler:
    order_handler = OrderCreatedHandler(channel, sms_channel)
    price_handler = PriceChangedHandler(channel)
    contact_handler = ContactSubmittedHandler(channel)
    newsletter_handler = NewsletterSubscribedHandler(channel)
    shipment_handler = ShipmentUpdateHandler(channel, sms_channel)
    settings_handler = SettingsChangedHandler(channel)
    system_error_handler = SystemErrorHandler(channel)
    fallback_handler = FallbackHandler()

    (
        order_handler.set_next(price_handler)
        .set_next(contact_handler)
        .set_next(newsletter_handler)
        .set_next(shipment_handler)
        .set_next(settings_handler)
        .set_next(system_error_handler)
        .set_next(fallback_handler)
    )
    return order_handler
