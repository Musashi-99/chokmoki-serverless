"""Reusable CLI to (re)assign an admin's scopes — for cases not yet covered
by an admin-panel UI action (root can also do this via PUT
/api/admin/admins/{id}/scopes once logged in). Root's own scopes can't be
set (always wildcard).

Primary use case: migrating already-invited admins onto a new default scope
set after a permission-taxonomy change (see src/models/admin_rbac.py's
ROLE_PERMISSIONS) — e.g. after adding products:price_write/coupons:read/
coupons:write and narrowing REGIONAL_ADMIN's default scopes, existing
regional_admin accounts still have their OLD stored scopes until migrated.

Usage (inside the backend container):
    # single admin, explicit scopes
    python scripts/set_admin_scopes.py --email sourav@example.com --scopes orders:read,coupons:read,coupons:write          # dry run
    python scripts/set_admin_scopes.py --email sourav@example.com --scopes orders:read,coupons:read,coupons:write --apply  # write

    # bulk: every admin with a given role, onto that role's current
    # ROLE_PERMISSIONS default (no --scopes needed)
    python scripts/set_admin_scopes.py --role regional_admin              # dry run
    python scripts/set_admin_scopes.py --role regional_admin --apply       # write
"""
import argparse
import asyncio
import sys

sys.path.insert(0, "/app")

from src.database.connection import db  # noqa: E402
from src.models.admin_rbac import ROLE_PERMISSIONS  # noqa: E402
from src.services.admin_user_service import AdminUserError, AdminUserService, RootAccountImmutableError  # noqa: E402


async def _migrate_one(service: AdminUserService, doc: dict, scopes: list[str], apply: bool) -> None:
    print(f"{'Setting' if apply else 'Would set'} scopes for {doc['email']} ({doc.get('role')}): {doc.get('scopes')} -> {scopes}")
    if not apply:
        return
    try:
        await service.set_scopes(str(doc["_id"]), scopes)
    except (AdminUserError, RootAccountImmutableError) as exc:
        print(f"  Failed for {doc['email']}: {exc}", file=sys.stderr)


async def run_single(email: str, scopes_csv: str, apply: bool) -> None:
    database = await db.get_database()
    doc = await database["admin_users"].find_one({"email": email.strip().lower()})
    if not doc:
        print(f"No admin found with email {email}", file=sys.stderr)
        sys.exit(1)

    scopes = [s.strip() for s in scopes_csv.split(",") if s.strip()]
    service = AdminUserService()
    await _migrate_one(service, doc, scopes, apply)
    if not apply:
        print("\nDry run only — re-run with --apply to write changes.")
    else:
        print("Done.")


async def run_by_role(role: str, apply: bool) -> None:
    scopes = sorted(ROLE_PERMISSIONS.get(role, set()))
    if not scopes:
        print(f"No ROLE_PERMISSIONS entry for role '{role}' (or it's empty)", file=sys.stderr)
        sys.exit(1)

    database = await db.get_database()
    docs = await database["admin_users"].find({"role": role, "is_root": {"$ne": True}}).to_list(length=None)
    if not docs:
        print(f"No non-root admins found with role '{role}'.")
        return

    service = AdminUserService()
    for doc in docs:
        await _migrate_one(service, doc, scopes, apply)

    if not apply:
        print(f"\nDry run only — {len(docs)} admin(s) would be updated. Re-run with --apply to write changes.")
    else:
        print(f"Done — migrated {len(docs)} admin(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--email", help="Migrate a single admin's scopes (requires --scopes)")
    group.add_argument("--role", help="Migrate every non-root admin with this role onto ROLE_PERMISSIONS' current default")
    parser.add_argument("--scopes", help="Comma-separated scope values, e.g. orders:read,coupons:write (required with --email)")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = parser.parse_args()

    if args.email:
        if args.scopes is None:
            parser.error("--scopes is required with --email")
        asyncio.run(run_single(args.email, args.scopes, apply=args.apply))
    else:
        asyncio.run(run_by_role(args.role, apply=args.apply))
