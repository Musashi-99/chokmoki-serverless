"""Meta catalog payload building — what gets pushed when a product is synced,
and that inactive products are removed instead of left purchasable."""
import os, sys
os.environ["ENVIRONMENT"] = "development"
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "secret")
os.environ["FB_CATALOG_ID"] = "123"
os.environ["FB_CATALOG_ACCESS_TOKEN"] = "token"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

from src.services.facebook_catalog_service import FacebookCatalogService, build_catalog_item


def _product(**overrides):
    defaults = dict(
        id="6a240979b9dbeec73e10d0e7",
        slug="golden-wing-bee-pendants",
        name="Golden Wing Bee pendants",
        description="Pavé-set wings.",
        thumbnail="https://cdn.example/a.webp",
        gallery=["https://cdn.example/a.webp", "https://cdn.example/b.webp"],
        material="925 Sterling Silver",
        category="pendants",
        price_inr=1100.0,
        prices=[
            SimpleNamespace(country="IN", mrp=1500.0, sellingPrice=1100.0),
            SimpleNamespace(country="AU", mrp=22.0, sellingPrice=19.0),
        ],
        stock=[SimpleNamespace(country="IN", qty=5, status="in_stock")],
        active=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_item_uses_india_inr_price_and_sale_price():
    item = build_catalog_item(_product())
    assert item["id"] == "6a240979b9dbeec73e10d0e7"
    assert item["price"] == "1500.00 INR"
    assert item["sale_price"] == "1100.00 INR"
    assert item["availability"] == "in stock"
    assert item["link"].endswith("/product/golden-wing-bee-pendants")
    assert item["additional_image_link"] == ["https://cdn.example/b.webp"]


def test_no_sale_price_when_not_discounted():
    p = _product(prices=[SimpleNamespace(country="IN", mrp=1100.0, sellingPrice=1100.0)])
    assert "sale_price" not in build_catalog_item(p)


def test_sold_out_india_is_out_of_stock():
    p = _product(stock=[SimpleNamespace(country="IN", qty=0, status="in_stock")])
    assert build_catalog_item(p)["availability"] == "out of stock"
    p = _product(stock=[SimpleNamespace(country="IN", qty=9, status="out_of_stock")])
    assert build_catalog_item(p)["availability"] == "out of stock"


def test_inactive_product_is_deleted_from_catalog():
    req = FacebookCatalogService._request_for(_product(active=False))
    assert req == {"method": "DELETE", "data": {"id": "6a240979b9dbeec73e10d0e7"}}


def test_active_product_is_upserted():
    req = FacebookCatalogService._request_for(_product())
    assert req["method"] == "UPDATE"
    assert req["data"]["title"] == "Golden Wing Bee pendants"
