"""GET /api/og/product/{slug} — live OG tags for link-preview crawlers
(WhatsApp/Discord/Facebook/etc.). See api/routes/storefront.py's
_og_meta_html/api_og_product docstrings for why this exists: those
crawlers never execute JS, so a plain React SPA can't give them real
per-product content. This must query the live database (via a short
cache-aside), not anything baked at build/deploy time, so an edited or
deleted product is reflected quickly — not "since the last deploy"."""
import os, sys
os.environ["ENVIRONMENT"] = "development"
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from api.routes.storefront import api_og_product


def _product(**overrides):
    defaults = dict(
        name="Eternal Heart Solitaire Ring",
        slug="eternal-heart-solitaire-ring",
        description="A hand-set solitaire in 92.5 sterling silver.",
        thumbnail="https://cdn.chokmoki.com/products/eternal-heart.jpg",
        active=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_returns_cached_html_without_hitting_the_database():
    with patch("api.routes.storefront.cache") as mock_cache, \
         patch("api.routes.storefront.ProductService") as mock_service_cls:
        mock_cache.get = AsyncMock(return_value="<html>cached</html>")
        response = await api_og_product("eternal-heart-solitaire-ring")
        mock_service_cls.assert_not_called()
        assert response.status_code == 200
        assert response.body == b"<html>cached</html>"


@pytest.mark.asyncio
async def test_live_product_renders_real_title_description_and_image_and_writes_through_cache():
    with patch("api.routes.storefront.cache") as mock_cache, \
         patch("api.routes.storefront.ProductService") as mock_service_cls:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        mock_service_cls.return_value.get_by_slug = AsyncMock(return_value=_product())

        response = await api_og_product("eternal-heart-solitaire-ring")

        assert response.status_code == 200
        body = response.body.decode()
        assert "Eternal Heart Solitaire Ring" in body
        assert "A hand-set solitaire in 92.5 sterling silver." in body
        assert 'og:image' in body
        assert "https://cdn.chokmoki.com/products/eternal-heart.jpg" in body
        assert "summary_large_image" in body
        assert "/product/eternal-heart-solitaire-ring" in body
        mock_cache.set.assert_awaited_once()
        cache_args, _ = mock_cache.set.call_args
        assert cache_args[0] == "chokmoki:og:product:eternal-heart-solitaire-ring"


@pytest.mark.asyncio
async def test_deleted_or_missing_product_returns_404_with_generic_fallback_and_does_not_cache():
    with patch("api.routes.storefront.cache") as mock_cache, \
         patch("api.routes.storefront.ProductService") as mock_service_cls:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        mock_service_cls.return_value.get_by_slug = AsyncMock(return_value=None)

        response = await api_og_product("no-longer-exists")

        assert response.status_code == 404
        body = response.body.decode()
        assert "no longer available" in body.lower()
        # Stable static file (public/, never content-hashed) — a real
        # fallback image instead of no image at all for a dead link.
        assert "android-chrome-512x512.png" in body
        mock_cache.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_deactivated_product_is_treated_the_same_as_missing():
    with patch("api.routes.storefront.cache") as mock_cache, \
         patch("api.routes.storefront.ProductService") as mock_service_cls:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        mock_service_cls.return_value.get_by_slug = AsyncMock(return_value=_product(active=False))

        response = await api_og_product("eternal-heart-solitaire-ring")

        assert response.status_code == 404
        mock_cache.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_description_is_truncated():
    with patch("api.routes.storefront.cache") as mock_cache, \
         patch("api.routes.storefront.ProductService") as mock_service_cls:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        long_desc = "x" * 400
        mock_service_cls.return_value.get_by_slug = AsyncMock(
            return_value=_product(description=long_desc)
        )

        response = await api_og_product("eternal-heart-solitaire-ring")

        body = response.body.decode()
        assert "x" * 297 + "..." in body
        assert "x" * 298 not in body.replace("...", "")


@pytest.mark.asyncio
async def test_missing_description_falls_back_to_a_generated_line():
    with patch("api.routes.storefront.cache") as mock_cache, \
         patch("api.routes.storefront.ProductService") as mock_service_cls:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        mock_service_cls.return_value.get_by_slug = AsyncMock(
            return_value=_product(description="")
        )

        response = await api_og_product("eternal-heart-solitaire-ring")

        body = response.body.decode()
        assert "sterling silver" in body.lower()


@pytest.mark.asyncio
async def test_missing_thumbnail_omits_og_image_and_uses_summary_card():
    with patch("api.routes.storefront.cache") as mock_cache, \
         patch("api.routes.storefront.ProductService") as mock_service_cls:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        mock_service_cls.return_value.get_by_slug = AsyncMock(
            return_value=_product(thumbnail="")
        )

        response = await api_og_product("eternal-heart-solitaire-ring")

        body = response.body.decode()
        assert "og:image" not in body
        assert 'content="summary"' in body


@pytest.mark.asyncio
async def test_title_and_description_are_html_escaped():
    with patch("api.routes.storefront.cache") as mock_cache, \
         patch("api.routes.storefront.ProductService") as mock_service_cls:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        mock_service_cls.return_value.get_by_slug = AsyncMock(
            return_value=_product(name='Ring <script>alert(1)</script> & Co')
        )

        response = await api_og_product("eternal-heart-solitaire-ring")

        body = response.body.decode()
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


@pytest.mark.asyncio
async def test_no_meta_refresh_tag():
    """Regression: a <meta http-equiv="refresh"> here was actively
    harmful, not just unnecessary — a real person never lands on this
    endpoint at all (nginx only routes known bot user-agents to it,
    never a real browser's), but WhatsApp's scraper was confirmed live
    to follow it as a redirect and re-scrape the plain app shell instead
    of using these tags — its preview showed the generic site title/
    domain rather than the real product, while Discord (which ignores
    the tag) showed everything correctly from the exact same response."""
    with patch("api.routes.storefront.cache") as mock_cache, \
         patch("api.routes.storefront.ProductService") as mock_service_cls:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        mock_service_cls.return_value.get_by_slug = AsyncMock(return_value=_product())

        response = await api_og_product("eternal-heart-solitaire-ring")

        assert "http-equiv" not in response.body.decode().lower()
