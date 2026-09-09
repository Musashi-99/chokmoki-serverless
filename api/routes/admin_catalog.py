"""Admin product + category CRUD."""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from typing import Any, Dict, List, Optional
import json
from api.bootstrap import AdminPrincipal, CategoryService, JewelryCategoryCreate, JewelryCategoryUpdate, JewelryProductCreate, JewelryProductUpdate, ProductService, build_update_payload, cache, require_admin, require_update_fields, require_any_scope, require_scope_email
from api.json_utils import JSONEncoder
from src.models.region import normalize_region_codes
from src.security.abac import is_allowed

router = APIRouter()

# `price_inr` is always in the allow-list alongside `prices` even though
# it's not something a price-only admin should independently control: it's
# deterministically auto-derived by JewelryProductUpdate._sync_legacy_inr
# from the `prices` list's IN row, so Pydantic marks it "set" and it lands
# in update_data on ANY prices edit that includes an IN row — even one that
# leaves IN completely unchanged. The real gate is the per-row loop below,
# which independently checks the IN row's actual old-vs-new values against
# the admin's allowed countries; price_inr can never diverge from that.
#
# `stock` is included too — a regional admin needs to mark their own
# region's stock in/out, not just its price (this is the operational
# reality of running a region: pricing without inventory control isn't
# enough). Same per-row-country enforcement as `prices` below.
_PRICE_ONLY_ALLOWED_FIELDS = {"prices", "price_inr", "stock"}


def _assert_price_only_edit(principal: AdminPrincipal, existing: Any, update_data: Dict[str, Any]) -> None:
    """A products:price_write-only admin (no full products:write) may submit
    ONLY `prices`/`stock`, and within each may only change rows whose
    country is one of their assigned region(s); every other row must be
    resubmitted identical to its current value. Enforced server-side
    regardless of what the UI sends — a real security boundary, not a UI
    courtesy."""
    extra_fields = set(update_data.keys()) - _PRICE_ONLY_ALLOWED_FIELDS
    if extra_fields:
        raise HTTPException(
            status_code=403,
            detail=f"Your access only allows editing prices/stock; cannot change: {sorted(extra_fields)}",
        )
    if "prices" not in update_data and "stock" not in update_data:
        raise HTTPException(status_code=403, detail="No price/stock fields to update")

    allowed_countries = set(normalize_region_codes(list(principal.regions))) if principal.regions else set()

    if "prices" in update_data:
        existing_prices_by_country = {p.country: p for p in (existing.prices if existing else [])}
        for row in update_data["prices"]:
            country = row["country"] if isinstance(row, dict) else row.country
            new_selling = row["sellingPrice"] if isinstance(row, dict) else row.sellingPrice
            new_mrp = row["mrp"] if isinstance(row, dict) else row.mrp
            old = existing_prices_by_country.get(country)
            changed = old is None or old.sellingPrice != new_selling or old.mrp != new_mrp
            if changed and country not in allowed_countries:
                raise HTTPException(
                    status_code=403,
                    detail=f"Your access does not allow editing the '{country}' price",
                )

    if "stock" in update_data:
        existing_stock_by_country = {s.country: s for s in (existing.stock if existing else [])}
        for row in update_data["stock"]:
            country = row["country"] if isinstance(row, dict) else row.country
            new_qty = row["qty"] if isinstance(row, dict) else row.qty
            new_status = row["status"] if isinstance(row, dict) else row.status
            old = existing_stock_by_country.get(country)
            changed = old is None or old.qty != new_qty or old.status != new_status
            if changed and country not in allowed_countries:
                raise HTTPException(
                    status_code=403,
                    detail=f"Your access does not allow editing the '{country}' stock",
                )


@router.get("/api/admin/products")
async def admin_list_products(
    skip: int = 0,
    limit: int = 200,
    search: Optional[str] = None,
    category: Optional[str] = None,
    active: Optional[bool] = None,
    is_best_seller: Optional[bool] = None,
    is_curated: Optional[bool] = None,
    email: str = Depends(require_scope_email("products", "read")),
):
    """List every product (including inactive) for the admin dashboard."""
    if ProductService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    service = ProductService()
    products = await service.list(
        skip=skip, limit=limit, active=active,
        category=category, is_best_seller=is_best_seller, is_curated=is_curated, search=search,
    )
    total = await service.count(
        active=active, category=category, is_best_seller=is_best_seller, is_curated=is_curated, search=search
    )
    return JSONResponse(content=json.loads(json.dumps({
        "data": products,
        "count": total,
    }, cls=JSONEncoder)))


@router.post("/api/admin/products")
async def admin_create_product(
    payload: Dict[str, Any], email: str = Depends(require_scope_email("products", "write"))
):
    """Create a product. Media URLs should already point to R2 (see /api/admin/upload)."""
    if ProductService is None or JewelryProductCreate is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    try:
        product_data = JewelryProductCreate(**payload)
        product = await ProductService().create(product_data)
    except HTTPException:
        raise
    except ValueError as e:
        if "already exists" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    if cache:
        await cache.delete_pattern("chokmoki:products:*")
        await cache.delete_pattern("chokmoki:product:*")

    return JSONResponse(content=json.loads(json.dumps(
        product.model_dump(by_alias=True), cls=JSONEncoder
    )))


@router.get("/api/admin/products/{product_id}")
async def admin_get_product(product_id: str, email: str = Depends(require_scope_email("products", "read"))):
    """Get a single product by MongoDB id."""
    if ProductService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    product = await ProductService().get_by_id(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return JSONResponse(content=json.loads(json.dumps(
        product.model_dump(by_alias=True), cls=JSONEncoder
    )))


@router.put("/api/admin/products/{product_id}")
async def admin_update_product(
    product_id: str,
    payload: Dict[str, Any],
    principal: AdminPrincipal = Depends(require_any_scope(("products", "write"), ("products", "price_write"))),
):
    """Update a product by its MongoDB id. Admins with only products:price_write
    (not full products:write) may change the `prices` array only, and only
    rows for their own assigned region(s) — see _assert_price_only_edit."""
    if ProductService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    service = ProductService()
    existing = await service.get_by_id(product_id)
    try:
        update_data = build_update_payload(JewelryProductUpdate, payload)
        require_update_fields(update_data)
        has_full_write = is_allowed(principal, "products", "write")
        if not has_full_write:
            _assert_price_only_edit(principal, existing, update_data)
        updated = await service.update(product_id, update_data, actor_email=principal.email)
    except HTTPException:
        raise
    except ValueError as e:
        if "already exists" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="Product not found")

    if cache:
        if existing and existing.slug != updated.slug:
            await cache.delete(f"chokmoki:product:{existing.slug}")
        await cache.delete_pattern("chokmoki:products:*")
        await cache.delete_pattern("chokmoki:product:*")

    return JSONResponse(content=json.loads(json.dumps(
        updated.model_dump(by_alias=True), cls=JSONEncoder
    )))


@router.delete("/api/admin/products/{product_id}")
async def admin_delete_product(product_id: str, email: str = Depends(require_scope_email("products", "write"))):
    """Delete a product by its MongoDB id."""
    if ProductService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    service = ProductService()
    existing = await service.get_by_id(product_id)
    try:
        deleted = await service.delete(product_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Product not found")

    if cache:
        if existing:
            await cache.delete(f"chokmoki:product:{existing.slug}")
        await cache.delete_pattern("chokmoki:products:*")
        await cache.delete_pattern("chokmoki:product:*")

    return {"success": True}


@router.get("/api/admin/categories")
async def admin_list_categories(email: str = Depends(require_scope_email("products", "read"))):
    """List every category for the admin dashboard."""
    if CategoryService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    service = CategoryService()
    categories = await service.list(limit=100)
    return JSONResponse(content=json.loads(json.dumps({
        "data": [cat.model_dump(by_alias=True) for cat in categories],
        "count": len(categories),
    }, cls=JSONEncoder)))


@router.post("/api/admin/categories")
async def admin_create_category(
    payload: Dict[str, Any], email: str = Depends(require_scope_email("products", "write"))
):
    """Create a category."""
    if CategoryService is None or JewelryCategoryCreate is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    try:
        category_data = JewelryCategoryCreate(**payload)
        category = await CategoryService().create(category_data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if cache:
        await cache.delete("chokmoki:categories")
        await cache.delete_pattern("chokmoki:category:*")

    return JSONResponse(content=json.loads(json.dumps(
        category.model_dump(by_alias=True), cls=JSONEncoder
    )))


_CATEGORY_UPDATE_FIELDS = frozenset({
    "slug", "name", "tagline", "banner", "thumbnail", "description", "sort_order", "active",
})


def _category_update_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if JewelryCategoryUpdate is None:
        return {k: v for k, v in payload.items() if k in _CATEGORY_UPDATE_FIELDS}
    update_data = build_update_payload(JewelryCategoryUpdate, payload)
    require_update_fields(update_data)
    return update_data


@router.put("/api/admin/categories/{category_id}")
async def admin_update_category(
    category_id: str, payload: Dict[str, Any], email: str = Depends(require_scope_email("products", "write"))
):
    """Update a category by its MongoDB id."""
    if CategoryService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    try:
        update_data = _category_update_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        updated = await CategoryService().update(category_id, update_data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="Category not found")

    if cache:
        await cache.delete("chokmoki:categories")
        await cache.delete_pattern("chokmoki:category:*")

    return JSONResponse(content=json.loads(json.dumps(
        updated.model_dump(by_alias=True), cls=JSONEncoder
    )))


@router.delete("/api/admin/categories/{category_id}")
async def admin_delete_category(category_id: str, email: str = Depends(require_scope_email("products", "write"))):
    """Delete a category by its MongoDB id."""
    if CategoryService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    try:
        deleted = await CategoryService().delete(category_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Category not found")

    if cache:
        await cache.delete("chokmoki:categories")
        await cache.delete_pattern("chokmoki:category:*")

    return {"success": True}
