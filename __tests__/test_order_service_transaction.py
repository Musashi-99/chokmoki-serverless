import os, sys
os.environ["ENVIRONMENT"] = "development"
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.order_service import OrderService


@pytest.mark.asyncio
async def test_order_insert_failure_does_not_leave_inventory_committed():
    product = MagicMock()
    product.id = "p1"
    product.name = "Ring"
    product.price_inr = 1000.0
    product.active = True
    # No per-region prices configured — create_from_admin() falls back to
    # price_inr when a product has no `prices` rows at all (an empty list,
    # not a truthy-but-unusable MagicMock, matching what a real product
    # with no MarketPrice rows looks like).
    product.prices = []

    with patch("src.services.order_service.ProductService") as mock_prod_cls, \
         patch("src.services.order_service.InventoryService") as mock_inv_cls, \
         patch("src.services.order_service.db") as mock_db, \
         patch("src.services.order_service.settings") as mock_settings, \
         patch.object(OrderService, "_validate_variant", return_value=True), \
         patch.object(OrderService, "_normalize_admin_payload", side_effect=lambda p: p):
        mock_settings.order_min_quantity = 1
        mock_settings.order_max_quantity = 10
        mock_settings.fraud_enabled = False

        mock_prod_cls.return_value.get_by_id = AsyncMock(return_value=product)
        inv_instance = mock_inv_cls.return_value
        inv_instance.commit_items = AsyncMock()
        inv_instance.release_committed_items = AsyncMock()

        mock_collection = AsyncMock()
        mock_collection.insert_one = AsyncMock(side_effect=RuntimeError("mongo down"))
        mock_db.get_database = AsyncMock(
            return_value={"orders": mock_collection, "order_logs": AsyncMock()}
        )

        service = OrderService()
        with pytest.raises(RuntimeError):
            await service.create_from_admin(
                payload={
                    "user_email": "buyer@example.com",
                    "shipping_address": {
                        "full_name": "A",
                        "phone": "1",
                        "address_line1": "x",
                        "city": "y",
                    },
                    "items": [{"product_id": "p1", "quantity": 1}],
                    "payment_status": "completed",
                },
                ip="127.0.0.1",
            )
        inv_instance.release_committed_items.assert_awaited_once()
