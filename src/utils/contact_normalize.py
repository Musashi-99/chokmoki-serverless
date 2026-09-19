"""Dedup keys for contact identifiers typed mid-checkout.

These are deliberately *approximate* — their only job is to fold the many
ways the same person types the same email/phone into one lookup key so the
abandoned-cart record doesn't fan out into a dozen near-duplicates. They
are NOT validation (the route does that separately) and must never be used
to decide whether a contact is reachable.
"""

from __future__ import annotations

from typing import Optional


def normalize_email(value: Optional[str]) -> Optional[str]:
    """Lowercase + trim. Nothing cleverer (no gmail dot/plus folding) —
    two addresses that differ only by a plus-tag are genuinely different
    inboxes, and collapsing them would merge two real people's carts."""
    if not value:
        return None
    normalized = value.strip().lower()
    return normalized or None


def normalize_phone(value: Optional[str]) -> Optional[str]:
    """Digits only, keeping the last 10 when longer.

    Folds `+91 98765 43210`, `919876543210` and `09876543210` to the same
    key. Approximate by design: for a country whose national numbers are
    shorter or longer than 10 digits this can, in theory, collide two
    different subscribers — acceptable for a dedup key on a marketing
    record, and the alternative (a full libphonenumber parse) needs a
    country hint we don't have while someone is still typing.
    """
    if not value:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    return digits[-10:] if len(digits) > 10 else digits
