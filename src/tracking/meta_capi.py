"""Meta Conversions API — server-side Purchase event.

The browser fires Purchase through GTM with eventID = order id; this module
sends the same event from the server with `event_id = order_id`, so Meta
keeps one and the ad platform still gets the sale when the browser tag is
blocked (ad blockers, Safari, a closed tab before the thank-you page).

Rules that matter for correctness:
- Never block or fail an order: callers use `schedule_purchase` (fire and
  forget) and every error is swallowed here.
- Exactly once: a Redis SET NX key per event_id (shared by the API and the
  worker) stops a second send; Redis being down only disables that guard.
- Customer data is SHA-256 hashed after Meta's normalization rules; fbp,
  fbc, IP and user agent are sent raw, as Meta requires."""
import asyncio
import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import httpx

from src.config import settings
from src.database.redis_connection import redis_client
from src.plugins.logger import logger
from src.resilience.retry import retry_async
from src.services.system_log_service import SystemLogService

DEDUPE_TTL_SECONDS = 7 * 24 * 3600
DEDUPE_KEY_PREFIX = "chokmoki:capi:purchase:"
GRAPH_TIMEOUT_SECONDS = 10.0
# Meta rejects events older than 7 days; stay safely inside that.
MAX_EVENT_AGE_SECONDS = 6 * 24 * 3600

_COUNTRY_ISO = {
    "india": "in", "australia": "au", "new zealand": "nz",
    "united states": "us", "usa": "us", "united kingdom": "gb", "uk": "gb",
    "canada": "ca", "singapore": "sg", "united arab emirates": "ae", "uae": "ae",
}
_DIAL_CODE = {"in": "91", "au": "61", "nz": "64"}

_background_tasks: Set["asyncio.Task[None]"] = set()


class _TransientCapiError(Exception):
    pass


def is_enabled() -> bool:
    return bool(settings.meta_dataset_id and _token())


def _token() -> str:
    return settings.meta_capi_token or settings.fb_catalog_access_token


def sha256_hex(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest() if cleaned else None


def country_iso(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    value = raw.strip().lower()
    if len(value) == 2 and value.isalpha():
        return value
    return _COUNTRY_ISO.get(value)


def normalize_phone(phone: Optional[str], country: Optional[str]) -> Optional[str]:
    """Digits only, with the country dial code (Meta wants it included)."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return None
    dial = _DIAL_CODE.get(country or "")
    if dial and not digits.startswith(dial):
        digits = dial + digits.lstrip("0")
    elif dial and digits.startswith(dial) and len(digits) <= len(dial) + 8:
        digits = dial + digits[len(dial):].lstrip("0")
    return digits


def split_name(full_name: Optional[str]) -> tuple:
    parts = (full_name or "").strip().split()
    if not parts:
        return None, None
    return parts[0], (" ".join(parts[1:]) or None)


def _event_time(created_at: Any, now: float) -> int:
    ts = None
    if isinstance(created_at, datetime):
        ts = created_at.timestamp()
    elif isinstance(created_at, str):
        try:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = None
    if ts is None or now - ts > MAX_EVENT_AGE_SECONDS or ts > now:
        return int(now)
    return int(ts)


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def build_purchase_event(order: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    address = order.get("shipping_address") or {}
    audit = order.get("region_audit") or {}
    tracking = order.get("tracking") or {}
    country = country_iso(address.get("country")) or country_iso(audit.get("ip_country"))
    first, last = split_name(address.get("full_name"))
    email = address.get("email") or order.get("user_email")

    user_data = _clean({
        "em": sha256_hex(email),
        "ph": sha256_hex(normalize_phone(address.get("phone"), country)),
        "fn": sha256_hex(first),
        "ln": sha256_hex(last),
        "ct": sha256_hex(re.sub(r"[^a-z]", "", (address.get("city") or "").lower())),
        "st": sha256_hex(re.sub(r"[^a-z]", "", (address.get("state") or "").lower())),
        "zp": sha256_hex(re.sub(r"\s", "", address.get("postal_code") or "")),
        "country": sha256_hex(country),
        "external_id": sha256_hex(order.get("user_id")),
        "client_ip_address": audit.get("ip"),
        "client_user_agent": audit.get("user_agent"),
        "fbp": tracking.get("fbp"),
        "fbc": tracking.get("fbc"),
    })

    items: List[Dict[str, Any]] = order.get("items") or []
    contents = [
        {
            "id": str(i.get("product_id")),
            "quantity": int(i.get("quantity") or 1),
            "item_price": float(i.get("unit_price") or 0),
        }
        for i in items
        if i.get("product_id")
    ]
    custom_data = _clean({
        "currency": (order.get("currency") or "INR").upper(),
        "value": round(float(order.get("total_amount") or 0), 2),
        "content_type": "product",
        "content_ids": [c["id"] for c in contents],
        "contents": contents,
        "num_items": sum(c["quantity"] for c in contents),
        "order_id": order.get("order_id"),
    })
    # value 0 is falsy for _clean's purposes but is still a valid, meaningful value
    custom_data.setdefault("value", round(float(order.get("total_amount") or 0), 2))

    return {
        "event_name": "Purchase",
        "event_time": _event_time(order.get("created_at"), now),
        "event_id": str(order["order_id"]),
        "action_source": "website",
        "event_source_url": tracking.get("event_source_url") or f"{settings.frontend_url.rstrip('/')}/thank-you",
        "user_data": user_data,
        "custom_data": custom_data,
    }


async def _claim(event_id: str) -> bool:
    """True if this process may send the event (first to claim it)."""
    try:
        redis = await redis_client.get_client()
        return bool(await redis.set(f"{DEDUPE_KEY_PREFIX}{event_id}", "1", nx=True, ex=DEDUPE_TTL_SECONDS))
    except Exception as e:
        if logger:
            logger.warning(f"CAPI duplicate guard unavailable (Redis): {e}")
        return True  # degrade: still send, only the duplicate guard is off


async def _release(event_id: str) -> None:
    try:
        redis = await redis_client.get_client()
        await redis.delete(f"{DEDUPE_KEY_PREFIX}{event_id}")
    except Exception:
        pass


async def _post_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    form = {"access_token": _token(), "data": json.dumps(events)}
    if settings.meta_test_code:
        form["test_event_code"] = settings.meta_test_code
    url = f"https://graph.facebook.com/{settings.meta_api_version}/{settings.meta_dataset_id}/events"
    async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, data=form)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 500 or resp.status_code == 429:
        raise _TransientCapiError(f"Meta CAPI HTTP {resp.status_code}")
    if resp.status_code >= 400 or "error" in body:
        message = (body.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
        raise ValueError(f"Meta CAPI rejected the event: {message}")
    return body


async def send_purchase(order: Dict[str, Any]) -> bool:
    """Send one Purchase. Never raises. True only if Meta accepted it."""
    if not is_enabled() or not order.get("order_id"):
        return False
    event_id = str(order["order_id"])
    if not await _claim(event_id):
        return False  # already sent (or being sent) — webhook retry / second worker
    try:
        event = build_purchase_event(order)
        body = await retry_async(
            lambda: _post_events([event]),
            retries=3, base_delay=1.0, max_delay=8.0,
            retryable_exceptions=(httpx.HTTPError, _TransientCapiError),
        )
        if body.get("events_received", 0) < 1:
            raise ValueError("Meta CAPI accepted the request but received 0 events")
        return True
    except Exception as e:
        await _release(event_id)
        if logger:
            logger.warning(f"Meta CAPI Purchase failed for order {event_id}: {e}")
        try:
            await SystemLogService().log(
                component="meta_capi", level="warning",
                message=f"Purchase event not delivered to Meta: {e}",
                context={"order_id": event_id},
            )
        except Exception:
            pass
        return False


def schedule_purchase(order: Dict[str, Any]) -> None:
    """Fire and forget — checkout must never wait on, or fail because of, Meta."""
    if not is_enabled():
        return
    try:
        task = asyncio.get_running_loop().create_task(send_purchase(dict(order)))
    except RuntimeError:
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
