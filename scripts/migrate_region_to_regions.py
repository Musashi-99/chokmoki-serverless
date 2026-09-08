"""One-time, idempotent schema migration: `admin_users.region` (single
string, or null) -> `admin_users.regions` (list of strings) — an admin can
now be assigned more than one region (e.g. one regional admin covering both
IN and AU). Safe to re-run: only touches docs that still have the old
`region` field and no `regions` field yet.

Usage (inside the backend container):
    python scripts/migrate_region_to_regions.py              # dry run
    python scripts/migrate_region_to_regions.py --apply       # write
"""
import argparse
import asyncio
import sys

sys.path.insert(0, "/app")

from src.database.connection import db  # noqa: E402
from src.models.region import normalize_region_codes  # noqa: E402

COLLECTION_NAME = "admin_users"


async def run(apply: bool) -> None:
    database = await db.get_database()
    collection = database[COLLECTION_NAME]

    cursor = collection.find({"regions": {"$exists": False}})
    total = 0
    async for doc in cursor:
        total += 1
        old_region = doc.get("region")
        new_regions = normalize_region_codes([old_region] if old_region else [])
        print(
            f"{'Migrating' if apply else 'Would migrate'} {doc['email']}: "
            f"region={old_region!r} -> regions={new_regions!r}"
        )
        if apply:
            await collection.update_one(
                {"_id": doc["_id"]},
                {"$set": {"regions": new_regions}, "$unset": {"region": ""}},
            )

    if total == 0:
        print("No admin_users docs need migrating — already on the `regions` schema.")
    elif apply:
        await collection.create_index("regions")
        print(f"\nMigrated {total} admin_users doc(s).")
    else:
        print(f"\n{total} admin_users doc(s) would be migrated — re-run with --apply to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply))
