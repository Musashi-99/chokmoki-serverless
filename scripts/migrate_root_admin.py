"""One-time, idempotent bootstrap: creates the single `is_root=true` row in
the new `admin_users` collection from the current env-configured admin
(ADMIN_EMAIL/ADMIN_PASSWORD[_HASH]/ADMIN_MFA_SECRET). Env vars are left
untouched afterwards — they remain root's break-glass fallback login path
(src/services/admin_auth_service.py's `authenticate()` never reads from
Mongo).

Usage (inside the backend container):
    python scripts/migrate_root_admin.py                # dry run
    python scripts/migrate_root_admin.py --apply         # write
"""
import argparse
import asyncio
import sys

sys.path.insert(0, "/app")

from src.config import settings  # noqa: E402
from src.database.connection import db  # noqa: E402
from src.models.admin_rbac import AdminRole  # noqa: E402
from src.security.password import hash_password  # noqa: E402
from src.security.secret_encryption import encrypt_totp_secret  # noqa: E402

COLLECTION_NAME = "admin_users"


async def run(apply: bool) -> None:
    database = await db.get_database()
    collection = database[COLLECTION_NAME]

    existing_root = await collection.find_one({"is_root": True})
    if existing_root:
        print(f"Root admin already exists ({existing_root['email']}) — nothing to do.")
        return

    email = (settings.admin_email or "").strip().lower()
    if not email:
        print("ADMIN_EMAIL is not configured — aborting.", file=sys.stderr)
        return

    password_hash = settings.admin_password_hash
    if not password_hash and settings.admin_password:
        password_hash = hash_password(settings.admin_password)

    totp_secret_encrypted = None
    totp_enrolled_at = None
    if settings.admin_mfa_secret:
        totp_secret_encrypted = encrypt_totp_secret(settings.admin_mfa_secret)
        from datetime import datetime

        totp_enrolled_at = datetime.utcnow()

    from datetime import datetime

    now = datetime.utcnow()
    doc = {
        "email": email,
        "name": "Root Admin",
        "role": AdminRole.SUPER_ADMIN.value,
        "scopes": ["*"],
        "regions": [],
        "status": "active",
        "is_root": True,
        "password_hash": password_hash,
        "totp_secret_encrypted": totp_secret_encrypted,
        "totp_enrolled_at": totp_enrolled_at,
        "telegram_chat_id": None,
        "created_by": "system-migration",
        "created_at": now,
        "updated_at": now,
        "last_login_at": None,
    }

    print(f"{'Inserting' if apply else 'Would insert'} root admin row for {email}.")
    if apply:
        await collection.create_index("email", unique=True)
        await collection.create_index("status")
        await collection.create_index("regions")
        await collection.insert_one(doc)
        print("Done.")
    else:
        print("\nDry run only — re-run with --apply to write changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply))
