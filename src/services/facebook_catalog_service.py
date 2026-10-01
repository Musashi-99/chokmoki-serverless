"""Push a product into the Meta (Facebook/Instagram) Commerce catalog.

Uses the catalog Batch API (`items_batch`, method UPDATE + allow_upsert) keyed
on retailer_id = our product id, so re-syncing an edited product updates the
same catalog item instead of creating a duplicate."""
import asyncio
import random
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from src.config import settings
from src.resilience.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

CATALOG_CURRENCY = "INR"
GRAPH_TIMEOUT_SECONDS = 20.0
BATCH_STATUS_POLLS = 4


# One breaker shared by the API and worker (state lives in Redis). Only
# transient failures count toward tripping it — a bad product payload must
# never take the whole integration offline.
FACEBOOK_BREAKER = CircuitBreaker(
    "facebook", failure_threshold=5, failure_window_seconds=60, cooldown_seconds=30
)

# Graph API error codes Meta documents as temporary / rate-limit related.
TRANSIENT_GRAPH_CODES = {1, 2, 4, 17, 32, 341, 368, 613, 80004}
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0
BACKOFF_JITTER = 0.3


class FacebookCatalogError(Exception):
    """`retryable` marks transient failures (rate limit, 5xx, network,
    breaker open) worth retrying with backoff; everything else (bad token,
    invalid data) fails fast. `retry_after` is a minimum wait in seconds."""

    def __init__(self, message: str, *, retryable: bool = False, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def backoff_delay(attempt: int) -> float:
    """Exponential (2s, 4s, 8s ... capped at 60s) plus up to 30% jitter, so
    many retries never hit Facebook in lockstep."""
    delay = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)
    return delay + random.uniform(0, BACKOFF_JITTER * delay)


def _in_price(product: Any) -> Dict[str, float]:
    """INR mrp/selling price — the catalog is INR and India is the only
    market currently accepting orders."""
    for p in product.prices or []:
        if p.country == "IN":
            return {"mrp": float(p.mrp), "selling": float(p.sellingPrice)}
    return {"mrp": float(product.price_inr), "selling": float(product.price_inr)}


def _availability(product: Any) -> str:
    rows = {s.country: s for s in (product.stock or [])}
    row = rows.get("IN") or rows.get("default")
    if row is None:
        return "in stock"
    if row.status == "out_of_stock" or (row.qty is not None and row.qty <= 0):
        return "out of stock"
    return "in stock"


def build_catalog_item(product: Any) -> Dict[str, Any]:
    price = _in_price(product)
    description = (product.description or "").strip() or (
        f"{product.name} — 92.5 sterling silver, handcrafted by Chokmoki in Kolkata."
    )
    item: Dict[str, Any] = {
        "id": str(product.id),
        "title": product.name,
        "description": description[:5000],
        "availability": _availability(product),
        "condition": "new",
        "price": f"{price['mrp']:.2f} {CATALOG_CURRENCY}",
        "link": f"{settings.frontend_url.rstrip('/')}/product/{product.slug}",
        "image_link": product.thumbnail,
        "brand": "Chokmoki",
        "material": product.material or None,
        "product_type": product.category or None,
    }
    if price["selling"] < price["mrp"]:
        item["sale_price"] = f"{price['selling']:.2f} {CATALOG_CURRENCY}"
    additional = [u for u in (product.gallery or []) if u and u != product.thumbnail][:10]
    if additional:
        item["additional_image_link"] = additional
    return {k: v for k, v in item.items() if v not in (None, "")}


BULK_CHUNK_SIZE = 500


def is_configured() -> bool:
    return bool(settings.fb_catalog_id and settings.fb_catalog_access_token)


class FacebookCatalogService:
    def __init__(self) -> None:
        if not is_configured():
            raise FacebookCatalogError("Facebook catalog is not configured on the server")
        self._base = f"https://graph.facebook.com/{settings.fb_graph_api_version}"
        self._catalog_id = settings.fb_catalog_id
        self._token = settings.fb_catalog_access_token

    @staticmethod
    def _request_for(product: Any) -> Dict[str, Any]:
        """Active products are upserted; inactive ones must not stay
        purchasable on Facebook, so they're removed from the catalog (a later
        reactivation + sync re-creates them)."""
        if not product.active:
            return {"method": "DELETE", "data": {"id": str(product.id)}}
        return {"method": "UPDATE", "data": build_catalog_item(product)}

    async def sync_product(self, product: Any) -> Dict[str, Any]:
        request = self._request_for(product)
        async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_SECONDS) as client:
            handle = await self._submit(client, [request])
            status = await self._poll_batch(client, handle)
        return {"retailer_id": str(product.id), "handle": handle, **status}

    async def sync_products(self, products: List[Any]) -> int:
        """Bulk resync (worker reconcile). Returns the number of items sent."""
        sent = 0
        for i in range(0, len(products), BULK_CHUNK_SIZE):
            chunk = products[i : i + BULK_CHUNK_SIZE]
            await self.submit_chunk(chunk)
            sent += len(chunk)
        return sent

    async def delete_product(self, product_id: str) -> None:
        async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_SECONDS) as client:
            await self._submit(client, [{"method": "DELETE", "data": {"id": str(product_id)}}])

    async def fetch_review_statuses(self) -> Dict[str, Dict[str, Any]]:
        """{retailer_id: {"status", "reasons"}} for every item in the catalog."""
        out: Dict[str, Dict[str, Any]] = {}
        url: Optional[str] = f"{self._base}/{self._catalog_id}/products"
        params: Optional[Dict[str, Any]] = {
            "access_token": self._token,
            "fields": "retailer_id,review_status,review_rejection_reasons",
            "limit": 200,
        }
        async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_SECONDS) as client:
            while url:
                resp = await client.get(url, params=params)
                body = _safe_json(resp)
                if resp.status_code >= 400 or "error" in body:
                    raise FacebookCatalogError(_graph_error(body, resp.status_code))
                for row in body.get("data") or []:
                    if row.get("retailer_id"):
                        out[row["retailer_id"]] = {
                            "status": row.get("review_status") or None,
                            "reasons": [str(r) for r in (row.get("review_rejection_reasons") or [])],
                        }
                url = (body.get("paging") or {}).get("next")
                params = None  # the `next` URL already carries every parameter
        return out

    async def submit_chunk(
        self,
        products: List[Any],
        *,
        on_retry: Optional[Callable[[int, float, "FacebookCatalogError"], Awaitable[None]]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> str:
        """Submit one batch, retrying transient failures with exponential
        backoff. `on_retry(attempt, delay, error)` runs before each wait;
        `sleep` is injectable so a job can heartbeat while it waits."""
        return await self.submit_with_retry(
            [self._request_for(p) for p in products], on_retry=on_retry, sleep=sleep
        )

    async def submit_with_retry(
        self,
        requests: List[Dict[str, Any]],
        *,
        on_retry: Optional[Callable[[int, float, "FacebookCatalogError"], Awaitable[None]]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> str:
        max_attempts = max(1, settings.fb_sync_max_attempts)
        attempt = 0
        while True:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_SECONDS) as client:
                    return await self._submit(client, requests)
            except FacebookCatalogError as e:
                if not e.retryable or attempt >= max_attempts:
                    raise
                delay = max(e.retry_after or 0.0, backoff_delay(attempt))
                if on_retry is not None:
                    await on_retry(attempt, delay, e)
                await sleep(delay)

    async def _submit(self, client: httpx.AsyncClient, requests: List[Dict[str, Any]]) -> str:
        async def _call() -> httpx.Response:
            resp = await client.post(
                f"{self._base}/{self._catalog_id}/items_batch",
                data={
                    "access_token": self._token,
                    "item_type": "PRODUCT_ITEM",
                    "allow_upsert": "true",
                    "requests": _json(requests),
                },
            )
            body = _safe_json(resp)
            if resp.status_code >= 400 or "error" in body:
                failure = _graph_failure(body, resp.status_code)
                if failure.retryable:
                    raise failure  # counts toward the breaker
            return resp

        try:
            resp = await FACEBOOK_BREAKER.call(_call)
        except CircuitBreakerOpenError:
            raise FacebookCatalogError(
                "Facebook calls are paused after repeated failures", retryable=True, retry_after=30.0
            )
        except httpx.HTTPError as e:
            raise FacebookCatalogError(
                f"Could not reach Facebook ({type(e).__name__})", retryable=True
            )
        body = _safe_json(resp)
        if resp.status_code >= 400 or "error" in body:
            raise _graph_failure(body, resp.status_code)
        handles: List[str] = body.get("handles") or []
        if not handles:
            raise FacebookCatalogError("Facebook accepted the request but returned no batch handle")
        return handles[0]

    async def _poll_batch(self, client: httpx.AsyncClient, handle: str) -> Dict[str, Any]:
        last: Dict[str, Any] = {"status": "pending", "errors": []}
        for _ in range(BATCH_STATUS_POLLS):
            await asyncio.sleep(1.5)
            resp = await client.get(
                f"{self._base}/{self._catalog_id}/check_batch_request_status",
                params={"access_token": self._token, "handle": handle, "load_ids_of_invalid_requests": "true"},
            )
            body = _safe_json(resp)
            if resp.status_code >= 400 or "error" in body:
                raise FacebookCatalogError(_graph_error(body, resp.status_code))
            data: Optional[Dict[str, Any]] = (body.get("data") or [None])[0]
            if not data:
                continue
            errors = data.get("errors") or []
            last = {"status": data.get("status", "unknown"), "errors": errors}
            if data.get("status") == "finished":
                return last
        return last


def _json(value: Any) -> str:
    import json
    return json.dumps(value)


def _safe_json(resp: httpx.Response) -> Dict[str, Any]:
    try:
        parsed = resp.json()
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def _graph_failure(body: Dict[str, Any], status_code: int) -> FacebookCatalogError:
    err = body.get("error") or {}
    retryable = (
        status_code >= 500
        or status_code == 429
        or err.get("is_transient") is True
        or err.get("code") in TRANSIENT_GRAPH_CODES
    )
    return FacebookCatalogError(_graph_error(body, status_code), retryable=retryable)


def _graph_error(body: Dict[str, Any], status_code: int) -> str:
    err = body.get("error") or {}
    return err.get("error_user_msg") or err.get("message") or f"Facebook API error (HTTP {status_code})"


async def refresh_review_statuses(product_service: Any) -> int:
    """Pull Facebook's approval verdicts into the opted-in products."""
    review = await FacebookCatalogService().fetch_review_statuses()
    await product_service.apply_facebook_review(review)
    return len(review)
