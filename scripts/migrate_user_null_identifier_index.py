"""Fixes the root cause of "new customer signup fails with Invalid or
expired OTP": the `users` collection's `phone`/`email` unique indexes were
`sparse=True`, which only excludes documents where the field is completely
ABSENT — not documents where the field is present with an explicit `null`.
The first phone-less (email-only) signup wrote `phone: null` and "claimed"
that slot in the unique index; every subsequent email-only signup then hit
a DuplicateKeyError creating their account. Because the OTP is deleted from
Redis as soon as it's verified (one-time use, src/services/otp_channel.py),
the failed account-creation left the user's — correct — OTP already
consumed, so retrying showed a confusing "Invalid or expired OTP" instead
of the real error (itself hidden by src/plugins/rate_limit.py's exception
handling swallowing it into an unlogged 503 — also fixed).

This script:
  1. Unsets `phone`/`email` where the value is explicitly null (idempotent
     — matches the field-omitted convention src/services/user_service.py's
     `_insert_doc()` now uses for new documents).
  2. Drops the old sparse unique indexes and creates the correct
     partialFilterExpression-based ones (same shape UserService.
     ensure_indexes() now creates going forward).

Usage (inside the backend container):
    python scripts/migrate_user_null_identifier_index.py              # dry run
    python scripts/migrate_user_null_identifier_index.py --apply       # write
"""
import argparse
import asyncio
import sys

sys.path.insert(0, "/app")

from src.database.connection import db  # noqa: E402

COLLECTION_NAME = "users"


async def run(apply: bool) -> None:
    database = await db.get_database()
    collection = database[COLLECTION_NAME]

    null_phone_count = await collection.count_documents({"phone": None})
    null_email_count = await collection.count_documents({"email": None})
    print(f"Docs with phone explicitly null: {null_phone_count}")
    print(f"Docs with email explicitly null: {null_email_count}")

    if apply:
        if null_phone_count:
            result = await collection.update_many({"phone": None}, {"$unset": {"phone": ""}})
            print(f"Unset phone on {result.modified_count} doc(s).")
        if null_email_count:
            result = await collection.update_many({"email": None}, {"$unset": {"email": ""}})
            print(f"Unset email on {result.modified_count} doc(s).")

        existing_indexes = await collection.index_information()
        for name in ("phone_1", "email_1"):
            if name in existing_indexes:
                await collection.drop_index(name)
                print(f"Dropped old index {name}.")

        await collection.create_index(
            "phone", unique=True, partialFilterExpression={"phone": {"$type": "string"}}
        )
        await collection.create_index(
            "email", unique=True, partialFilterExpression={"email": {"$type": "string"}}
        )
        print("Created partial unique indexes on phone/email.")
        print("\nDone.")
    else:
        print(
            "\nWould unset the null fields above and rebuild phone/email indexes as "
            "partial unique indexes — re-run with --apply to write."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply))
