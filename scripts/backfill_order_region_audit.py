"""Backfill `region_audit` for orders created before region tracking
existed (and, until the create_from_admin() fix, every manual/phone order
too) — the "ghost orders" the site owner asked to fix by tagging them with
a real region rather than deleting real order/revenue history.

Maps each order's `shipping_address.country` (free text, as typed by a
customer at checkout or an admin creating a manual order) to a region code
using the same small set this project already treats as real markets
(IN/AU/NZ), case-insensitively, tolerant of common variants. A value that
doesn't map to any known market (garbage data was found in production —
a bare number, an image URL) falls back to "default", exactly like an
unrecognized country at real checkout already does (src/pricing/resolvers.py).

Sets region_audit.pricing_country_used = region_audit.selected_country =
the mapped code, and stashes the original raw shipping_address.country
value + a `backfilled: true` marker in geoip_raw_response (the model's own
free-form evidence-trail field) — so any order this touches is honestly
distinguishable later from one that went through real GeoIP resolution at
checkout, not silently rewritten as if it always had that data.

Usage (inside the backend container):
    python scripts/backfill_order_region_audit.py              # dry run
    python scripts/backfill_order_region_audit.py --apply       # write
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, "/app")

from src.database.connection import db  # noqa: E402

COLLECTION_NAME = "orders"

# Case-insensitive country-name/synonym -> region code. Anything not
# listed here (including garbage values) maps to "default".
COUNTRY_NAME_TO_REGION = {
    "india": "IN",
    "in": "IN",
    "bharat": "IN",
    "australia": "AU",
    "au": "AU",
    "new zealand": "NZ",
    "nz": "NZ",
    "aotearoa": "NZ",
}


def _map_country(raw: object) -> str:
    text = str(raw or "").strip().lower()
    return COUNTRY_NAME_TO_REGION.get(text, "default")


async def run(apply: bool) -> None:
    database = await db.get_database()
    collection = database[COLLECTION_NAME]

    query = {"region_audit": None}
    total = await collection.count_documents(query)
    print(f"Orders with no region_audit: {total}")
    if total == 0:
        print("Nothing to do.")
        return

    mapping_counts: Counter[str] = Counter()
    rows = []
    async for doc in collection.find(query, {"order_id": 1, "shipping_address.country": 1}):
        raw_country = ((doc.get("shipping_address") or {}).get("country"))
        region = _map_country(raw_country)
        mapping_counts[region] += 1
        rows.append((doc["_id"], doc.get("order_id"), raw_country, region))

    print("\nMapping breakdown:")
    for region, count in sorted(mapping_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {region}: {count}")

    print("\nSample (first 10):")
    for _id, order_id, raw_country, region in rows[:10]:
        print(f"  {order_id}  {raw_country!r:40s} -> {region}")

    if not apply:
        print("\nDry run only — re-run with --apply to write changes.")
        return

    now = datetime.utcnow()
    updated = 0
    for _id, order_id, raw_country, region in rows:
        await collection.update_one(
            {"_id": _id},
            {"$set": {
                "region_audit": {
                    "ip": None,
                    "ip_country": None,
                    "ip_country_raw": None,
                    "selected_country": region,
                    "detected_country_at_bootstrap": None,
                    "pricing_country_used": region,
                    "country_mismatch": False,
                    "user_agent": None,
                    "geoip_raw_response": {
                        "backfilled": True,
                        "source": "shipping_address.country",
                        "raw_value": raw_country,
                    },
                    "resolved_at": now,
                }
            }},
        )
        updated += 1

    print(f"\nBackfilled region_audit on {updated} order(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply))
