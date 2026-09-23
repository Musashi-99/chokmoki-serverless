"""Service-level integration tests for PreorderService — real Mongo
connection (this project's established pattern for service-level tests,
see test_admin_order_region_pricing.py). Covers the upsert-on-resubmission
dedup key, region-scoped list/count filtering, and status-not-reset
behavior."""
from __future__ import annotations

import uuid

import pytest

from src.database.connection import db
from src.models.preorder import PreorderCreateInput
from src.services.preorder_service import PreorderService

COLLECTION = "preorders"


@pytest.fixture(autouse=True)
def _reset_mongo_singleton_per_test():
    from src.database.redis_connection import redis_client as _redis_client

    db._client = None
    _redis_client._client = None
    _redis_client._connection_pool = None
    yield
    db._client = None
    _redis_client._client = None
    _redis_client._connection_pool = None


def _unique_email() -> str:
    return f"preorder-test-{uuid.uuid4().hex[:10]}@example.com"


def _input(email: str, product_id: str, **overrides) -> PreorderCreateInput:
    defaults = dict(
        productId=product_id,
        name="Jane Doe",
        email=email,
        phone="+61400000000",
        quantity=2,
        size="M",
        notifyVia=["email"],
        message="please notify me",
    )
    defaults.update(overrides)
    return PreorderCreateInput(**defaults)


@pytest.fixture
async def cleanup_preorders():
    database = await db.get_database()
    inserted_ids = []
    yield inserted_ids
    if inserted_ids:
        await database[COLLECTION].delete_many({"_id": {"$in": inserted_ids}})


@pytest.mark.asyncio
class TestRecordUpsert:
    async def test_first_submission_creates_a_new_row_with_status_new(self, cleanup_preorders):
        service = PreorderService()
        email = _unique_email()
        doc = await service.record(
            _input(email, "prod-1"), product_name="Ring", product_slug="ring", region="AU"
        )
        cleanup_preorders.append(doc["_id"])
        assert doc["status"] == "new"
        assert doc["email_normalized"] == email.lower()
        assert doc["region"] == "AU"
        assert doc["quantity"] == 2

    async def test_resubmission_same_email_and_product_updates_the_same_row(
        self, cleanup_preorders
    ):
        service = PreorderService()
        email = _unique_email()
        first = await service.record(
            _input(email, "prod-1"), product_name="Ring", product_slug="ring", region="AU"
        )
        cleanup_preorders.append(first["_id"])

        second = await service.record(
            _input(email, "prod-1", quantity=5, size="L", message="updated message"),
            product_name="Ring",
            product_slug="ring",
            region="AU",
        )
        assert second["_id"] == first["_id"]
        assert second["quantity"] == 5
        assert second["size"] == "L"
        assert second["message"] == "updated message"

        database = await db.get_database()
        count = await database[COLLECTION].count_documents(
            {"email_normalized": email.lower(), "product_id": "prod-1"}
        )
        assert count == 1

    async def test_resubmission_does_not_reset_status_if_already_notified(
        self, cleanup_preorders
    ):
        service = PreorderService()
        email = _unique_email()
        first = await service.record(
            _input(email, "prod-1"), product_name="Ring", product_slug="ring", region="AU"
        )
        cleanup_preorders.append(first["_id"])

        await service.update_status(str(first["_id"]), "notified")

        second = await service.record(
            _input(email, "prod-1", quantity=3),
            product_name="Ring",
            product_slug="ring",
            region="AU",
        )
        assert second["status"] == "notified"
        assert second["quantity"] == 3

    async def test_different_product_same_email_creates_a_separate_row(self, cleanup_preorders):
        service = PreorderService()
        email = _unique_email()
        first = await service.record(
            _input(email, "prod-1"), product_name="Ring", product_slug="ring", region="AU"
        )
        second = await service.record(
            _input(email, "prod-2"), product_name="Necklace", product_slug="necklace", region="AU"
        )
        cleanup_preorders.extend([first["_id"], second["_id"]])
        assert first["_id"] != second["_id"]


@pytest.mark.asyncio
class TestRegionResolution:
    async def test_selected_country_in_supported_markets_wins(self, monkeypatch):
        from src.pricing import geo_provider as geo_provider_module

        # supported_countries() memoizes into a module-level global the
        # first time it's called — force it to recompute against this
        # test's expected market set rather than trusting whatever earlier
        # test/import cached first.
        monkeypatch.setattr(geo_provider_module, "_SUPPORTED", {"IN", "AU", "NZ"})

        class _FakeGeo:
            country = None
            raw_country = None
            raw = None

        class _FakeAdapter:
            async def lookup(self, ip):
                return _FakeGeo()

        monkeypatch.setattr(
            "src.services.preorder_service.GeoIPDiscoveryAdapter", _FakeAdapter
        )
        region = await PreorderService.resolve_region("AU", "1.2.3.4")
        assert region == "AU"

    async def test_arbitrary_client_supplied_region_string_cannot_be_spoofed(self, monkeypatch):
        """A client sending selectedCountry="ZZ" (not a real supported
        market) must never end up stored verbatim — it must fall through
        to GeoIP or 'default', exactly like checkout's own resolution."""
        from src.pricing import geo_provider as geo_provider_module

        monkeypatch.setattr(geo_provider_module, "_SUPPORTED", {"IN", "AU", "NZ"})

        class _FakeGeo:
            country = None
            raw_country = None
            raw = None

        class _FakeAdapter:
            async def lookup(self, ip):
                return _FakeGeo()

        monkeypatch.setattr(
            "src.services.preorder_service.GeoIPDiscoveryAdapter", _FakeAdapter
        )
        region = await PreorderService.resolve_region("ZZ", "1.2.3.4")
        assert region == "default"
        assert region != "ZZ"


@pytest.mark.asyncio
class TestAdminListRegionFiltering:
    async def test_regions_param_narrows_query_to_in_clause(self, cleanup_preorders):
        service = PreorderService()
        email_au = _unique_email()
        email_in = _unique_email()
        marker = f"RegionTestRing-{uuid.uuid4().hex[:8]}"
        au_doc = await service.record(
            _input(email_au, "prod-region-test"), product_name=marker, product_slug="ring",
            region="AU",
        )
        in_doc = await service.record(
            _input(email_in, "prod-region-test-2"), product_name=marker, product_slug="ring",
            region="IN",
        )
        cleanup_preorders.extend([au_doc["_id"], in_doc["_id"]])

        au_only = await service.list(regions=["AU"], search=marker)
        regions_seen = {d["region"] for d in au_only}
        assert regions_seen <= {"AU"}
        assert regions_seen == {"AU"}

        count = await service.count(regions=["AU"], search=marker)
        assert count == 1

    async def test_unrestricted_admin_query_returns_all_regions(self, cleanup_preorders):
        service = PreorderService()
        email = _unique_email()
        marker = f"UnrestrictedTestRing-{uuid.uuid4().hex[:8]}"
        doc = await service.record(
            _input(email, "prod-unrestricted-test"), product_name=marker, product_slug="ring",
            region="NZ",
        )
        cleanup_preorders.append(doc["_id"])

        results = await service.list(regions=None, search=marker)
        assert any(d["region"] == "NZ" for d in results)
