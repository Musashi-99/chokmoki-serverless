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
STOCK_EVENT_BACK_IN_STOCK = "back_in_stock"


def evaluate_stock_crossing(
    old_qty: Optional[int],
    new_qty: Optional[int],
    threshold: int,
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
) -> Optional[str]:
    """Returns which single alert (if any) this stock change just crossed
    into. Never more than one — a purchase that jumps straight from
    "plenty" to zero only ever reports STOCK_EVENT_OUT_OF_STOCK, not
    low-stock too.

    Status-driven first: `status` (in_stock/out_of_stock) is the field an
    admin directly toggles in the UI, independent of qty — an admin can
    flip a product's status without changing its qty at all (e.g. marking
    it unavailable for a reason qty doesn't capture), and that must alert
    just as much as a real purchase emptying it out. Any actual status
    flip reports immediately, regardless of what qty is doing.

    Falls back to a pure qty-threshold check only when status didn't
    change (or wasn't supplied, e.g. by an older caller) — this still
    catches a purchase that crosses the low-stock threshold without (yet)
    flipping status to out_of_stock. Returns None for restocks (qty
    increased/unchanged with no status change) and for a qty already
    at/below the threshold before this change (already alerted once when
    it first crossed — don't spam every subsequent purchase while it
    stays low). `old_qty`/`new_qty` of None means "untracked inventory" —
    the qty-threshold check never alerts on it, but a status flip still
    does (status is independent of whether qty is tracked at all).
    """
    if old_status != new_status and new_status is not None:
        if new_status == "out_of_stock":
            return STOCK_EVENT_OUT_OF_STOCK
        if new_status == "in_stock":
            return STOCK_EVENT_BACK_IN_STOCK

    if old_qty is None or new_qty is None:
        return None
    if new_qty >= old_qty:
        return None
    if new_qty <= 0 and old_qty > 0:
        return STOCK_EVENT_OUT_OF_STOCK
    if new_qty <= threshold and old_qty > threshold:
        return STOCK_EVENT_LOW_STOCK
    return None
