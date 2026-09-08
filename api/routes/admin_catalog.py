"""Admin product + category CRUD."""
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from typing import Any, Dict, List, Optional
import json
from api.bootstrap import CategoryService, JewelryCategoryCreate, JewelryCategoryUpdate, JewelryProductCreate, JewelryProductUpdate, ProductService, build_update_payload, cache, require_admin, require_update_fields, require_scope_email
from api.json_utils import JSONEncoder

router = APIRouter()


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
    product_id: str, payload: Dict[str, Any], email: str = Depends(require_scope_email("products", "write"))
):
    """Update a product by its MongoDB id."""
    if ProductService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    service = ProductService()
    existing = await service.get_by_id(product_id)
    try:
        update_data = build_update_payload(JewelryProductUpdate, payload)
        require_update_fields(update_data)
        updated = await service.update(product_id, update_data)
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
