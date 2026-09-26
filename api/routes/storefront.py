"""Public, read-only storefront content endpoints (no auth)."""
from html import escape as _html_escape
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
import json
from api.bootstrap import AccountPageSettingsService, BlogService, CategoryService, CollectionSlideService, ContactPageSettingsService, FAQItemService, GeoIPDiscoveryAdapter, HeroConfigService, HistoryPageSettingsService, HomePageSettingsService, NavigationSettingsService, PolicyContentService, ProductPageSettingsService, ProductService, ShopPageSettingsService, SiteAssetService, StoryPageSettingsService, StudioSettingsService, TestimonialService, cache, get_client_ip, settings
from api.json_utils import JSONEncoder, _json_dumps, _json_response_content
from src.models.product import MarketPrice, MarketStock
from src.pricing.price_lookup import resolve_price
from src.pricing.stock_lookup import resolve_stock
from src.pricing.resolvers import resolve_country
from src.services.product_filters import parse_ids_query

router = APIRouter()

SELECTED_COUNTRY_HEADER = "x-selected-country"


async def _effective_country(request: Request) -> str:
    """Resolve the pricing country for this request: explicit selection
    (header, set by the frontend from its Zustand store) first, GeoIP on
    the request's IP second, "default" as the terminal fallback."""
    selected = request.headers.get(SELECTED_COUNTRY_HEADER) or request.query_params.get("country")
    ip_country = None
    if get_client_ip and GeoIPDiscoveryAdapter:
        ip = get_client_ip(request)
        geo = await GeoIPDiscoveryAdapter().lookup(ip)
        ip_country = geo.country
    return resolve_country(selected_country=selected, ip_country=ip_country)


def _apply_market_pricing(product: dict, country: str) -> dict:
    """Attach the region-resolved price onto a product response, in place —
    the frontend keeps reading `price_inr`/new `currency`/`sym`/`mrp` fields
    as one already-resolved price, same as before multi-region existed."""
    raw_prices = product.get("prices") or []
    if not raw_prices:
        return product
    prices = [MarketPrice(**p) for p in raw_prices]
    match = resolve_price(prices, country)
    product["price_inr"] = match.sellingPrice
    product["mrp"] = match.mrp
    product["currency"] = match.currency
    product["sym"] = match.sym
    product["pricing_country"] = match.country
    return product


def _apply_market_stock(product: dict, country: str) -> dict:
    """Attach the region-resolved stock_status/stock_qty onto a product
    response, in place — mirrors _apply_market_pricing. Stock is tracked
    per-region (src/models/product.py: MarketStock): a product can be sold
    out in India while still in stock for Australia, so the frontend must
    see the number for *this* customer's region, not a single global flag.
    Untracked products (no `stock` rows at all) are left with no
    stock_status/stock_qty — always purchasable, same as before."""
    raw_stock = product.get("stock") or []
    if not raw_stock:
        return product
    stock = [MarketStock(**s) for s in raw_stock]
    match = resolve_stock(stock, country)
    if match is None:
        return product
    product["stock_status"] = match.status
    product["stock_qty"] = match.qty
    return product


async def _cache_products_key(category, search, sort, skip, limit, is_best_seller=None, is_curated=None, ids=None, country=None):
    return (
        f"chokmoki:products:{category or 'all'}:{search or 'all'}:{sort or 'default'}"
        f":bs{is_best_seller}:cur{is_curated}:{skip}:{limit}:ids{ids or 'all'}:c{country or 'default'}"
    )


@router.get("/api/products")
async def api_list_products(
    request: Request,
    category: Optional[str] = None,
    search: Optional[str] = None,
    sort: Optional[str] = None,
    is_best_seller: Optional[bool] = None,
    is_curated: Optional[bool] = None,
    ids: Optional[str] = Query(default=None),
    skip: int = 0,
    limit: int = 50,
):
    """List products with optional filtering (cached)"""
    if ProductService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    id_list = parse_ids_query(ids)
    if id_list:
        limit = max(limit, len(id_list))

    country = await _effective_country(request)

    cache_key = await _cache_products_key(
        category, search, sort, skip, limit, is_best_seller, is_curated, ",".join(id_list) if id_list else None,
        country,
    )
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = ProductService()
    products = await service.list(
        skip=skip, limit=limit, active=True,
        category=category, sort=sort, search=search,
        is_best_seller=is_best_seller, is_curated=is_curated,
        ids=id_list,
    )
    products = [_apply_market_stock(_apply_market_pricing(p, country), country) for p in products]
    total = await service.count(
        active=True, category=category, search=search,
        is_best_seller=is_best_seller, is_curated=is_curated,
        ids=id_list,
    )
    result = {"data": products, "count": total}

    if cache:
        await cache.set(cache_key, _json_dumps(result), 300)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/products/{slug}")
async def api_get_product(slug: str, request: Request):
    """Get a single product by slug"""
    if ProductService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    country = await _effective_country(request)
    cache_key = f"chokmoki:product:{slug}:c{country}"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = ProductService()
    product = await service.get_by_slug(slug)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    result = json.loads(json.dumps(
        product.model_dump(by_alias=True),
        cls=JSONEncoder
    ))
    result = _apply_market_stock(_apply_market_pricing(result, country), country)
    if cache:
        await cache.set(cache_key, _json_dumps(result), 300)

    return JSONResponse(content=result)


def _og_meta_html(*, title: str, description: str, image: Optional[str], canonical_url: str) -> str:
    """Minimal static HTML — just the tags a link-preview crawler reads.
    Not the SPA: WhatsApp/Discord/Facebook/etc.'s scrapers never execute
    JS, so this is the only way they ever see real per-product content
    (see nginx's $chokmoki_og_bot map, which routes ONLY known bot
    user-agents to this endpoint for /product/<slug> — everyone else
    gets the normal app, unchanged).

    Deliberately NO <meta http-equiv="refresh">: a real person never
    lands here at all (their own browser's user-agent doesn't match the
    bot map, so nginx never routes them to this endpoint — they go
    straight to the real SPA when they click through). A refresh tag
    only risked WhatsApp's scraper following it as a redirect and
    re-scraping the plain app shell instead of using these tags —
    confirmed live: WhatsApp's preview showed the generic site title/
    domain instead of the real product title, while Discord (which
    doesn't follow it) showed everything correctly from the exact same
    response."""
    t = _html_escape(title)
    d = _html_escape(description)
    u = _html_escape(canonical_url)
    image_tags = ""
    if image:
        img = _html_escape(image)
        image_tags = (
            f'<meta property="og:image" content="{img}">\n'
            f'    <meta name="twitter:image" content="{img}">\n'
            f'    <meta name="twitter:card" content="summary_large_image">'
        )
    else:
        image_tags = '<meta name="twitter:card" content="summary">'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{t}</title>
    <meta name="description" content="{d}">
    <link rel="canonical" href="{u}">
    <meta property="og:type" content="product">
    <meta property="og:title" content="{t}">
    <meta property="og:description" content="{d}">
    <meta property="og:url" content="{u}">
    <meta property="og:site_name" content="Chokmoki">
    {image_tags}
</head>
<body>
    <p><a href="{u}">{t}</a></p>
</body>
</html>"""


OG_CACHE_TTL_SECONDS = 600  # 10 min — bounds DB load on a viral share
# without meaningfully delaying how fast an edited/deleted product's
# preview goes stale, unlike a build-time-baked snapshot (which would
# only ever update on the next deploy).


@router.get("/api/og/product/{slug}", response_class=HTMLResponse)
async def api_og_product(slug: str):
    """Live, per-request OG tags for a product — see _og_meta_html's
    docstring for why this exists and who actually hits it. Queries the
    real database (via a short cache-aside, not baked at build/deploy
    time), so a product that's edited or deactivated/deleted is reflected
    here within OG_CACHE_TTL_SECONDS, not "since the last deploy"."""
    cache_key = f"chokmoki:og:product:{slug}"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return HTMLResponse(content=cached, status_code=200)

    product = await ProductService().get_by_slug(slug) if ProductService else None
    canonical_url = f"{settings.frontend_url}/product/{slug}"

    if not product or not product.active:
        # A deleted/deactivated product's link should show as "gone", not
        # stale wrong info — generic site branding + 404, same posture as
        # the prerendered 404 snapshot the storefront itself falls back to
        # for a genuinely missing route. The brand logo here is a static
        # public/ file (android-chrome-512x512.png, never content-hashed
        # by the frontend build, unlike the fingerprinted files under
        # /assets/) — a fixed URL that keeps working across frontend
        # deploys, and a plain file read, no image-resize compute.
        html = _og_meta_html(
            title="Chokmoki — Sterling Silver Jewellery",
            description="This piece is no longer available. Explore the current collection at Chokmoki.",
            image=f"{settings.frontend_url}/android-chrome-512x512.png",
            canonical_url=f"{settings.frontend_url}/products",
        )
        return HTMLResponse(content=html, status_code=404)

    description = (product.description or "").strip()
    if len(description) > 300:
        description = description[:297].rstrip() + "..."
    if not description:
        description = f"{product.name} — 92.5 sterling silver, handcrafted by Chokmoki in Kolkata."

    html = _og_meta_html(
        title=f"{product.name} — Chokmoki",
        description=description,
        image=product.thumbnail or None,
        canonical_url=canonical_url,
    )
    if cache:
        await cache.set(cache_key, html, OG_CACHE_TTL_SECONDS)
    return HTMLResponse(content=html, status_code=200)


@router.get("/api/categories")
async def api_list_categories():
    """List all active categories (cached)"""
    if CategoryService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")
    
    cache_key = "chokmoki:categories"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))
    
    service = CategoryService()
    categories = await service.list(active=True)
    total = await service.count(active=True)
    result = {
        "data": [cat.model_dump(by_alias=True) for cat in categories],
        "count": total
    }
    
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)
    
    return JSONResponse(content=_json_response_content(result))


@router.get("/api/categories/{slug}")
async def api_get_category(slug: str):
    """Get a single category by slug"""
    if CategoryService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = f"chokmoki:category:{slug}"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = CategoryService()
    category = await service.get_by_slug(slug)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")

    result = json.loads(json.dumps(
        category.model_dump(by_alias=True),
        cls=JSONEncoder
    ))
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=result)

@router.get("/api/testimonials")
async def api_list_testimonials():
    """List all active testimonials."""
    if TestimonialService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:testimonials"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = TestimonialService()
    testimonials = await service.list(active=True)
    total = await service.count(active=True)

    result = {"data": testimonials, "count": total}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 300)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/hero")
async def api_get_hero_config():
    """Get the active hero configuration."""
    if HeroConfigService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:hero"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = HeroConfigService()
    config = await service.get_active()
    if not config:
        result = {"media_type": None, "media_url": None, "alt_text": None, "active": False}
        if cache:
            await cache.set(cache_key, _json_dumps(result), 300)
        return JSONResponse(content=result)

    result = config.model_dump(by_alias=True)
    if cache:
        await cache.set(cache_key, _json_dumps(result), 300)

    return JSONResponse(content=_json_response_content(result))

@router.get("/api/site-assets")
async def api_list_site_assets():
    """List all active site assets."""
    if SiteAssetService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:site-assets"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = SiteAssetService()
    assets = await service.list(active=True)
    result = {"data": assets, "count": len(assets)}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/site-assets/{key}")
async def api_get_site_asset(key: str):
    """Get a single active site asset by key."""
    if SiteAssetService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = f"chokmoki:site-asset:{key}"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = SiteAssetService()
    asset = await service.get_by_key(key)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    result = json.loads(json.dumps(
        asset.model_dump(by_alias=True),
        cls=JSONEncoder
    ))
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=result)


# ========== Public: FAQ ==========

@router.get("/api/faq")
async def api_list_faq(scope: Optional[str] = None):
    """List active FAQ items, optionally filtered by scope."""
    if FAQItemService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = f"chokmoki:faq:{scope or 'all'}"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = FAQItemService()
    items = await service.list(scope=scope, active=True)
    result = {"data": items, "count": len(items)}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 300)

    return JSONResponse(content=_json_response_content(result))


# ========== Public: Collection Slides ==========

@router.get("/api/collection-slides")
async def api_list_collection_slides():
    """List all active collection slides."""
    if CollectionSlideService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:collection-slides"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = CollectionSlideService()
    slides = await service.list(active=True)
    result = {"data": slides, "count": len(slides)}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 300)

    return JSONResponse(content=_json_response_content(result))

# ========== Public: Studio, Shop page, Policies ==========

@router.get("/api/studio-settings")
async def api_get_studio_settings():
    if StudioSettingsService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:studio-settings"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    data = await StudioSettingsService().get_public()
    result = {"data": data}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/shop-page")
async def api_get_shop_page():
    if ShopPageSettingsService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:shop-page"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    data = await ShopPageSettingsService().get_public()
    result = {"data": data}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/policies")
async def api_get_policies():
    if PolicyContentService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:policies"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    bundle = await PolicyContentService().get_public_bundle()
    if cache:
        await cache.set(cache_key, _json_dumps(bundle), 600)

    return JSONResponse(content=_json_response_content(bundle))

@router.get("/api/home-page")
async def api_get_home_page():
    if HomePageSettingsService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:home-page"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    data = await HomePageSettingsService().get_public()
    result = {"data": data}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/story-page")
async def api_get_story_page():
    if StoryPageSettingsService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:story-page"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    data = await StoryPageSettingsService().get_public()
    result = {"data": data}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/navigation")
async def api_get_navigation():
    if NavigationSettingsService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:navigation"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    data = await NavigationSettingsService().get_public()
    result = {"data": data}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/contact-page")
async def api_get_contact_page():
    if ContactPageSettingsService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:contact-page"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    data = await ContactPageSettingsService().get_public()
    result = {"data": data}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/account-page")
async def api_get_account_page():
    if AccountPageSettingsService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:account-page"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    data = await AccountPageSettingsService().get_public()
    result = {"data": data}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/history-page")
async def api_get_history_page():
    if HistoryPageSettingsService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:history-page"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    data = await HistoryPageSettingsService().get_public()
    result = {"data": data}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/product-page")
async def api_get_product_page():
    if ProductPageSettingsService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:product-page"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    data = await ProductPageSettingsService().get_public()
    result = {"data": data}
    if cache:
        await cache.set(cache_key, _json_dumps(result), 600)

    return JSONResponse(content=_json_response_content(result))


@router.get("/api/journal")
async def api_get_journal():
    if BlogService is None:
        raise HTTPException(status_code=500, detail="Server not initialized")

    cache_key = "chokmoki:journal"
    if cache:
        cached = await cache.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached))

    service = BlogService()
    meta = await service.get_journal_public()
    posts = await service.list_posts(active=True, limit=50)
    result = {
        "meta": meta,
        "data": posts,
        "count": len(posts),
    }
    if cache:
        await cache.set(cache_key, _json_dumps(result), 300)

    return JSONResponse(content=_json_response_content(result))
