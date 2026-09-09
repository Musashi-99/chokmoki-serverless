"""CRUD + lifecycle for the `admin_users` Mongo collection.

Kept separate from AdminAuthService (token/session lifecycle for whoever is
already authenticating) — this is identity/lifecycle management for the
admin_users collection itself, mirroring the user.py/user_service.py split
already used for customers.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime
from typing import Optional

import pyotp
from bson import ObjectId
from bson.errors import InvalidId

from src.config import settings
from src.database.connection import db
from src.database.redis_connection import redis_client
from src.models.admin_rbac import AdminPermission, AdminRole
from src.models.region import is_valid_region, normalize_region_codes
from src.security.login_lockout import LoginLockoutService
from src.security.secret_encryption import decrypt_totp_secret, encrypt_totp_secret

COLLECTION_NAME = "admin_users"
INVITE_PREFIX = "admin:invite:"
INVITE_TTL_SECONDS = 30 * 60
TOTP_ISSUER = "Chokmoki Admin"

# In-process cache for Telegram-routing lookups (src/alerts/handlers.py) —
# alert dispatch runs per event, not per HTTP request, so a short TTL avoids
# hammering Mongo without needing a Redis round trip for something this
# low-stakes (a stale routing target for at most 60s is harmless).
_ROUTING_CACHE_TTL_SECONDS = 60
_routing_cache: dict[str, tuple[float, Optional[dict]]] = {}


class AdminUserError(Exception):
    """Base class for admin_users lifecycle errors surfaced as 400s."""


class RootAccountImmutableError(AdminUserError):
    def __init__(self) -> None:
        super().__init__("The root admin account cannot be modified via this API")


def _to_object_id(admin_id: str) -> ObjectId:
    try:
        return ObjectId(admin_id)
    except (InvalidId, TypeError) as exc:
        raise AdminUserError("Invalid admin id") from exc


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AdminUserService:
    def __init__(self) -> None:
        self.totp_lockout = LoginLockoutService(kind="admin_totp")

    async def ensure_indexes(self) -> None:
        database = await db.get_database()
        collection = database[COLLECTION_NAME]
        await collection.create_index("email", unique=True)
        await collection.create_index("status")
        await collection.create_index("regions")

    # ---- reads --------------------------------------------------------

    async def get_by_email(self, email: str) -> Optional[dict]:
        database = await db.get_database()
        return await database[COLLECTION_NAME].find_one(
            {"email": (email or "").strip().lower()}
        )

    async def get_by_id(self, admin_id: str) -> Optional[dict]:
        database = await db.get_database()
        return await database[COLLECTION_NAME].find_one({"_id": _to_object_id(admin_id)})

    async def list_admins(self) -> list[dict]:
        database = await db.get_database()
        cursor = database[COLLECTION_NAME].find({}).sort("created_at", -1)
        return [doc async for doc in cursor]

    async def get_region_chat_id(self, region: str) -> Optional[str]:
        """Resolve the region's designated REGIONAL_ADMIN's own
        telegram_chat_id as the "region chat" — reuses admin_users rather
        than a second collection. Cached briefly since this runs per alert
        dispatch (src/alerts/handlers.py)."""
        if not region:
            return None
        cache_key = f"region:{region}"
        cached = _routing_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < _ROUTING_CACHE_TTL_SECONDS:
            return cached[1].get("telegram_chat_id") if cached[1] else None

        database = await db.get_database()
        # Equality against an array field matches "region is one of the
        # elements" — no $in/$elemMatch needed for a scalar comparison.
        doc = await database[COLLECTION_NAME].find_one(
            {
                "regions": region,
                "role": AdminRole.REGIONAL_ADMIN.value,
                "telegram_chat_id": {"$nin": [None, ""]},
                "status": "active",
            }
        )
        _routing_cache[cache_key] = (now, doc)
        return doc.get("telegram_chat_id") if doc else None

    async def get_routing_for_actor(self, email: str) -> Optional[dict]:
        """Cached admin_users lookup used by Telegram alert routing."""
        cache_key = f"email:{(email or '').strip().lower()}"
        cached = _routing_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < _ROUTING_CACHE_TTL_SECONDS:
            return cached[1]

        doc = await self.get_by_email(email)
        _routing_cache[cache_key] = (now, doc)
        return doc

    # ---- invite / enrollment -------------------------------------------

    async def create_invite(
        self,
        *,
        email: str,
        name: str,
        role: str,
        scopes: list[str],
        regions: Optional[list[str]],
        created_by: str,
    ) -> tuple[dict, str]:
        email = (email or "").strip().lower()
        if not email:
            raise AdminUserError("Email is required")

        regions = normalize_region_codes(regions)
        for code in regions:
            if not is_valid_region(code):
                raise AdminUserError(f"Unknown region: {code}")
        if role == AdminRole.REGIONAL_ADMIN.value and not regions:
            raise AdminUserError("At least one region is required for the Regional Admin role")

        database = await db.get_database()
        collection = database[COLLECTION_NAME]
        existing = await collection.find_one({"email": email})
        if existing:
            raise AdminUserError(f"An admin with email {email} already exists")

        now = datetime.utcnow()
        doc = {
            "email": email,
            "name": name,
            "role": role,
            "scopes": scopes,
            "regions": regions,
            "status": "invited",
            "is_root": False,
            "password_hash": None,
            "totp_secret_encrypted": None,
            "totp_enrolled_at": None,
            "telegram_chat_id": None,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
            "last_login_at": None,
        }
        result = await collection.insert_one(doc)
        doc["_id"] = result.inserted_id

        token = await self._issue_enrollment_token(email)
        return doc, token

    async def _issue_enrollment_token(self, email: str) -> str:
        secret = pyotp.random_base32()
        token = secrets.token_urlsafe(32)
        redis = await redis_client.get_client()
        await redis.setex(
            f"{INVITE_PREFIX}{_hash_token(token)}",
            INVITE_TTL_SECONDS,
            _pack_invite(email, secret),
        )
        return token

    async def get_invite(self, token: str) -> Optional[dict]:
        redis = await redis_client.get_client()
        raw = await redis.get(f"{INVITE_PREFIX}{_hash_token(token)}")
        if not raw:
            return None
        return _unpack_invite(raw)

    async def get_enrollment_info(self, token: str) -> Optional[dict]:
        invite = await self.get_invite(token)
        if not invite:
            return None
        totp = pyotp.TOTP(invite["secret"])
        provisioning_uri = totp.provisioning_uri(
            name=invite["email"], issuer_name=TOTP_ISSUER
        )
        return {"email": invite["email"], "provisioning_uri": provisioning_uri}

    async def confirm_enrollment(self, token: str, code: str) -> bool:
        invite = await self.get_invite(token)
        if not invite:
            return False

        totp = pyotp.TOTP(invite["secret"])
        if not code or not totp.verify(code.strip(), valid_window=1):
            return False

        database = await db.get_database()
        collection = database[COLLECTION_NAME]
        now = datetime.utcnow()
        result = await collection.update_one(
            {"email": invite["email"], "status": "invited"},
            {
                "$set": {
                    "totp_secret_encrypted": encrypt_totp_secret(invite["secret"]),
                    "totp_enrolled_at": now,
                    "status": "active",
                    "updated_at": now,
                }
            },
        )
        if result.matched_count == 0:
            return False

        redis = await redis_client.get_client()
        await redis.delete(f"{INVITE_PREFIX}{_hash_token(token)}")
        return True

    async def regenerate_totp(self, admin_id: str, *, actor_email: str) -> str:
        """Root-only (enforced by the route via require_scope + this
        service-layer guard). Clears the existing secret, flips status back
        to 'invited', and returns a fresh enrollment token so the admin can
        re-enroll from scratch."""
        doc = await self.get_by_id(admin_id)
        if not doc:
            raise AdminUserError("Admin not found")
        if doc.get("is_root"):
            raise RootAccountImmutableError()

        database = await db.get_database()
        now = datetime.utcnow()
        await database[COLLECTION_NAME].update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "totp_secret_encrypted": None,
                    "totp_enrolled_at": None,
                    "status": "invited",
                    "updated_at": now,
                }
            },
        )
        return await self._issue_enrollment_token(doc["email"])

    # ---- lifecycle ------------------------------------------------------

    async def deactivate(self, admin_id: str, *, actor_email: str) -> dict:
        doc = await self.get_by_id(admin_id)
        if not doc:
            raise AdminUserError("Admin not found")
        if doc.get("is_root"):
            raise RootAccountImmutableError()

        database = await db.get_database()
        now = datetime.utcnow()
        await database[COLLECTION_NAME].update_one(
            {"_id": doc["_id"]}, {"$set": {"status": "deactivated", "updated_at": now}}
        )
        doc["status"] = "deactivated"
        return doc

    async def reactivate(self, admin_id: str, *, actor_email: str) -> dict:
        doc = await self.get_by_id(admin_id)
        if not doc:
            raise AdminUserError("Admin not found")
        if doc.get("is_root"):
            raise RootAccountImmutableError()

        database = await db.get_database()
        now = datetime.utcnow()
        # Re-enrollment is not required to reactivate — the admin's TOTP
        # secret (if previously enrolled) is left intact.
        new_status = "active" if doc.get("totp_enrolled_at") else "invited"
        await database[COLLECTION_NAME].update_one(
            {"_id": doc["_id"]}, {"$set": {"status": new_status, "updated_at": now}}
        )
        doc["status"] = new_status
        return doc

    async def set_regions(self, admin_id: str, regions: Optional[list[str]]) -> dict:
        """Root-only region (re)assignment — used by
        scripts/set_admin_regions.py and available for a future admin-panel
        "edit regions" action. Root's own regions can't be set (always
        global/unrestricted)."""
        doc = await self.get_by_id(admin_id)
        if not doc:
            raise AdminUserError("Admin not found")
        if doc.get("is_root"):
            raise RootAccountImmutableError()

        normalized = normalize_region_codes(regions)
        for code in normalized:
            if not is_valid_region(code):
                raise AdminUserError(f"Unknown region: {code}")
        if doc.get("role") == AdminRole.REGIONAL_ADMIN.value and not normalized:
            raise AdminUserError("At least one region is required for the Regional Admin role")

        database = await db.get_database()
        now = datetime.utcnow()
        await database[COLLECTION_NAME].update_one(
            {"_id": doc["_id"]}, {"$set": {"regions": normalized, "updated_at": now}}
        )
        doc["regions"] = normalized
        return doc

    async def set_scopes(self, admin_id: str, scopes: Optional[list[str]]) -> dict:
        """Root-only scope (re)assignment — used by
        scripts/set_admin_scopes.py to migrate already-invited admins onto a
        new default scope set, and available for a future admin-panel "edit
        scopes" action. Root's own scopes can't be set (always wildcard)."""
        doc = await self.get_by_id(admin_id)
        if not doc:
            raise AdminUserError("Admin not found")
        if doc.get("is_root"):
            raise RootAccountImmutableError()

        normalized = sorted({(s or "").strip() for s in (scopes or []) if (s or "").strip()})
        known = {p.value for p in AdminPermission}
        for scope in normalized:
            if scope != "*" and scope not in known:
                raise AdminUserError(f"Unknown scope: {scope}")

        database = await db.get_database()
        now = datetime.utcnow()
        await database[COLLECTION_NAME].update_one(
            {"_id": doc["_id"]}, {"$set": {"scopes": normalized, "updated_at": now}}
        )
        doc["scopes"] = normalized
        return doc

    async def touch_last_login(self, email: str) -> None:
        database = await db.get_database()
        await database[COLLECTION_NAME].update_one(
            {"email": (email or "").strip().lower()},
            {"$set": {"last_login_at": datetime.utcnow()}},
        )

    # ---- TOTP verification (login) --------------------------------------

    async def verify_totp_login(
        self, email: str, code: Optional[str], *, client_ip: str
    ) -> bool:
        await self.totp_lockout.assert_not_locked(client_ip, email)

        doc = await self.get_by_email(email)
        if not doc or doc.get("status") != "active" or not doc.get("totp_secret_encrypted"):
            await self.totp_lockout.record_failure(client_ip, email)
            return False

        if not code:
            await self.totp_lockout.record_failure(client_ip, email)
            return False

        try:
            secret = decrypt_totp_secret(doc["totp_secret_encrypted"])
            ok = pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
        except Exception:
            ok = False

        if not ok:
            await self.totp_lockout.record_failure(client_ip, email)
            return False

        await self.totp_lockout.record_success(client_ip, email)
        return True


def _pack_invite(email: str, secret: str) -> str:
    import json

    return json.dumps({"email": email, "secret": secret})


def _unpack_invite(raw) -> dict:
    import json

    data = json.loads(raw)
    return {"email": data["email"], "secret": data["secret"]}
