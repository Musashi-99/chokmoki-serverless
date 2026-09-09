"""Unit tests for the pure stock-crossing rule
(src/services/stock_alerts.py) shared by both the purchase-decrement path
(inventory_service.py) and the admin manual-edit path (product_service.py)."""
from src.services.stock_alerts import (
    STOCK_EVENT_LOW_STOCK,
    STOCK_EVENT_OUT_OF_STOCK,
    evaluate_stock_crossing,
)

THRESHOLD = 10


def test_no_crossing_when_staying_well_above_threshold():
    assert evaluate_stock_crossing(50, 45, THRESHOLD) is None


def test_crossing_into_low_stock():
    assert evaluate_stock_crossing(11, 9, THRESHOLD) == STOCK_EVENT_LOW_STOCK


def test_crossing_exactly_onto_threshold_counts_as_low_stock():
    assert evaluate_stock_crossing(11, 10, THRESHOLD) == STOCK_EVENT_LOW_STOCK


def test_jumping_straight_to_zero_reports_out_of_stock_only():
    assert evaluate_stock_crossing(15, 0, THRESHOLD) == STOCK_EVENT_OUT_OF_STOCK


def test_already_below_threshold_purchase_does_not_alert_again():
    # Already crossed once at some earlier purchase — don't spam every sale.
    assert evaluate_stock_crossing(8, 5, THRESHOLD) is None


def test_already_out_of_stock_stays_silent():
    assert evaluate_stock_crossing(0, 0, THRESHOLD) is None


def test_restock_never_alerts():
    assert evaluate_stock_crossing(5, 20, THRESHOLD) is None


def test_unchanged_qty_never_alerts():
    assert evaluate_stock_crossing(20, 20, THRESHOLD) is None


def test_untracked_inventory_never_alerts():
    assert evaluate_stock_crossing(None, 5, THRESHOLD) is None
    assert evaluate_stock_crossing(5, None, THRESHOLD) is None
    assert evaluate_stock_crossing(None, None, THRESHOLD) is None


def test_out_of_stock_takes_precedence_when_already_at_zero_before():
    # old_qty <= 0 means no real decrement happened (already empty) — no
    # new crossing to report.
    assert evaluate_stock_crossing(-1, -5, THRESHOLD) is None
