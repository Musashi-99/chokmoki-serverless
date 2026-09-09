from __future__ import annotations

from typing import Any, Dict, Optional

from src.config import settings
from src.streams import event_bus

ALERTS_STREAM_KEY = "chokmoki:alerts:stream"

EVENT_ORDER_CREATED = "order.created"
EVENT_ADMIN_MUTATION = "admin.mutation"
EVENT_PRODUCT_PRICE_CHANGED = "product.price_changed"
# Fired by src/services/stock_alerts.py's evaluate_stock_crossing() — from
# both a real purchase decrementing stock (inventory_service.py) and an
# admin's manual stock edit (product_service.py) crossing the same rule.
EVENT_PRODUCT_OUT_OF_STOCK = "product.out_of_stock"
EVENT_PRODUCT_LOW_STOCK = "product.low_stock"
EVENT_PRODUCT_BACK_IN_STOCK = "product.back_in_stock"

# Maps src/services/stock_alerts.py's crossing markers (alerts-independent
# inventory-domain strings) to the Telegram event type each becomes — the
# one place both inventory_service.py and product_service.py look this up,
# so a third crossing kind added later only needs an entry here.
_STOCK_CROSSING_EVENT_MAP = {
    "out_of_stock": EVENT_PRODUCT_OUT_OF_STOCK,
    "low_stock": EVENT_PRODUCT_LOW_STOCK,
    "back_in_stock": EVENT_PRODUCT_BACK_IN_STOCK,
}


def event_type_for_stock_crossing(crossing: str) -> str:
    return _STOCK_CROSSING_EVENT_MAP[crossing]
EVENT_CONTACT_SUBMITTED = "contact.submitted"
EVENT_NEWSLETTER_SUBSCRIBED = "newsletter.subscribed"
EVENT_SHIPMENT_UPDATE = "shipment.updated"
# Fired by src/services/system_log_service.py for every level="error" entry
# (circuit breaker trips, stream DLQ drops, worker task crashes, orphaned
# payments) — the operational-error counterpart to the business alerts
# above, reusing the same Telegram channel instead of a separate mechanism.
EVENT_SYSTEM_ERROR = "system.error"


async def publish_alert(event_type: str, payload: Dict[str, Any]) -> Optional[str]:
    """Publish an alert-worthy event. No-ops (and never raises) if neither
    Telegram nor SMS (MSG91) is configured, so callers on a request's hot
    path never pay for Redis I/O when both channels are off.
    """
    if not settings.telegram_enabled and not settings.msg91_enabled:
        return None
    return await event_bus.publish(ALERTS_STREAM_KEY, event_type, payload)
