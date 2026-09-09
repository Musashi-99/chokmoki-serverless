"""Pure stock-level-crossing rule, shared by every place stock quantity
changes (purchase-driven decrements in src/services/inventory_service.py,
and admin manual edits in src/services/product_service.py) so the "when do
we alert" rule is written once and can't drift between the two paths.

Deliberately has zero dependency on the alerts subsystem (src/alerts/) —
this is inventory-domain logic, not Telegram-specific, so it stays reusable
if a future caller needs the same rule for something other than a Telegram
message (e.g. a reorder webhook).
"""
from __future__ import annotations

from typing import Optional

STOCK_EVENT_OUT_OF_STOCK = "out_of_stock"
STOCK_EVENT_LOW_STOCK = "low_stock"


def evaluate_stock_crossing(
    old_qty: Optional[int], new_qty: Optional[int], threshold: int
) -> Optional[str]:
    """Returns which single alert (if any) this qty change just crossed
    into. Never both — a purchase that jumps straight from "plenty" to
    zero only ever reports STOCK_EVENT_OUT_OF_STOCK, not low-stock too, so
    one purchase never produces two alerts. Returns None for restocks (qty
    increased or unchanged) and for a qty that was already at/below the
    threshold before this change (already alerted once when it first
    crossed — don't spam on every subsequent purchase while it stays low).
    `old_qty`/`new_qty` of None means "untracked inventory" — never alerts.
    """
    if old_qty is None or new_qty is None:
        return None
    if new_qty >= old_qty:
        return None
    if new_qty <= 0 and old_qty > 0:
        return STOCK_EVENT_OUT_OF_STOCK
    if new_qty <= threshold and old_qty > threshold:
        return STOCK_EVENT_LOW_STOCK
    return None
