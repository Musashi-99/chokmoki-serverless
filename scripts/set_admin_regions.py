"""Reusable CLI to (re)assign an admin's regions by email — for cases not
yet covered by an admin-panel UI action (root can also do this via PUT
/api/admin/admins/{id}/regions once logged in). Root's own regions can't be
set (always global/unrestricted).

Usage (inside the backend container):
    python scripts/set_admin_regions.py sourav@example.com IN,AU          # dry run
    python scripts/set_admin_regions.py sourav@example.com IN,AU --apply  # write
    python scripts/set_admin_regions.py sourav@example.com ""             # clears regions (dry run)
"""
import argparse
import asyncio
import sys

sys.path.insert(0, "/app")

from src.database.connection import db  # noqa: E402
from src.services.admin_user_service import AdminUserError, AdminUserService, RootAccountImmutableError  # noqa: E402


async def run(email: str, regions_csv: str, apply: bool) -> None:
    database = await db.get_database()
    doc = await database["admin_users"].find_one({"email": email.strip().lower()})
    if not doc:
        print(f"No admin found with email {email}", file=sys.stderr)
        sys.exit(1)

    regions = [r.strip() for r in regions_csv.split(",") if r.strip()]
    print(f"{'Setting' if apply else 'Would set'} regions for {doc['email']}: {doc.get('regions')} -> {regions}")

    if not apply:
        print("\nDry run only — re-run with --apply to write changes.")
        return

    service = AdminUserService()
    try:
        await service.set_regions(str(doc["_id"]), regions)
    except (AdminUserError, RootAccountImmutableError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("email", help="Admin's email address")
    parser.add_argument("regions", help="Comma-separated region codes, e.g. IN,AU (empty string clears)")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(run(args.email, args.regions, apply=args.apply))
