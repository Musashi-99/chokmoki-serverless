"""Push a product into the Meta (Facebook/Instagram) Commerce catalog.

Uses the catalog Batch API (`items_batch`, method UPDATE + allow_upsert) keyed
on retailer_id = our product id, so re-syncing an edited product updates the
same catalog item instead of creating a duplicate."""
import asyncio
from typing import Any, Dict, List, Optional

import httpx

from src.config import settings

CATALOG_CURRENCY = "INR"
GRAPH_TIMEOUT_SECONDS = 20.0
BATCH_STATUS_POLLS = 4


class FacebookCatalogError(Exception):
    pass


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
        async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_SECONDS) as client:
            for i in range(0, len(products), BULK_CHUNK_SIZE):
                chunk = products[i : i + BULK_CHUNK_SIZE]
                await self._submit(client, [self._request_for(p) for p in chunk])
                sent += len(chunk)
        return sent

    async def delete_product(self, product_id: str) -> None:
        async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_SECONDS) as client:
            await self._submit(client, [{"method": "DELETE", "data": {"id": str(product_id)}}])

    async def _submit(self, client: httpx.AsyncClient, requests: List[Dict[str, Any]]) -> str:
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
            raise FacebookCatalogError(_graph_error(body, resp.status_code))
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


def _graph_error(body: Dict[str, Any], status_code: int) -> str:
    err = body.get("error") or {}
    return err.get("error_user_msg") or err.get("message") or f"Facebook API error (HTTP {status_code})"
