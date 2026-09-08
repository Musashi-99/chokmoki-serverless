from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

from src.config import settings
from src.database.redis_connection import redis_client
from src.models.admin_auth import AuthTokens
from src.plugins.logger import logger


class AdminSessionService:
    SESSION_PREFIX = "admin:session:"
    REFRESH_PREFIX = "admin:refresh:"
    REVOKED_PREFIX = "admin:revoked:"
    USER_SESSIONS_PREFIX = "admin:user_sessions:"

    def _refresh_ttl_seconds(self) -> int:
        return settings.jwt_refresh_ttl_days * 24 * 60 * 60

    def _access_ttl_seconds(self) -> int:
        return settings.jwt_access_ttl_minutes * 60

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def create_session(self, email: str, role: str) -> tuple[str, str, str]:
        """Create a session. Returns (session_id, refresh_token_plain, csrf_token)."""
        session_id = str(uuid.uuid4())
        refresh_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        refresh_hash = self._hash_token(refresh_token)

        session_doc = {
            "email": email,
            "role": role,
            "refresh_token_hash": refresh_hash,
            "created_at": datetime.utcnow().isoformat(),
            "last_rotated_at": datetime.utcnow().isoformat(),
        }

        redis = await redis_client.get_client()
        pipe = redis.pipeline()
        pipe.setex(
            f"{self.SESSION_PREFIX}{session_id}",
            self._refresh_ttl_seconds(),
            json.dumps(session_doc),
        )
        pipe.setex(
            f"{self.REFRESH_PREFIX}{refresh_hash}",
            self._refresh_ttl_seconds(),
            session_id,
        )
        pipe.sadd(f"{self.USER_SESSIONS_PREFIX}{email.lower()}", session_id)
        pipe.expire(f"{self.USER_SESSIONS_PREFIX}{email.lower()}", self._refresh_ttl_seconds())
        await pipe.execute()

        return session_id, refresh_token, csrf_token

    async def get_session(self, session_id: str) -> Optional[dict]:
        redis = await redis_client.get_client()
        raw = await redis.get(f"{self.SESSION_PREFIX}{session_id}")
        if not raw:
            return None
        return json.loads(raw)

    async def is_jti_revoked(self, jti: str) -> bool:
        redis = await redis_client.get_client()
        return bool(await redis.exists(f"{self.REVOKED_PREFIX}{jti}"))

    async def revoke_jti(self, jti: str, ttl_seconds: int) -> None:
        if ttl_seconds < 1:
            ttl_seconds = 1
        redis = await redis_client.get_client()
        await redis.setex(f"{self.REVOKED_PREFIX}{jti}", ttl_seconds, "1")

    async def revoke_session(self, session_id: str) -> None:
        redis = await redis_client.get_client()
        raw = await redis.get(f"{self.SESSION_PREFIX}{session_id}")
        if not raw:
            return

        session = json.loads(raw)
        email = session.get("email", "").lower()
        refresh_hash = session.get("refresh_token_hash")

        pipe = redis.pipeline()
        pipe.delete(f"{self.SESSION_PREFIX}{session_id}")
        if refresh_hash:
            pipe.delete(f"{self.REFRESH_PREFIX}{refresh_hash}")
        if email:
            pipe.srem(f"{self.USER_SESSIONS_PREFIX}{email}", session_id)
        await pipe.execute()

    async def revoke_all_user_sessions(self, email: str) -> int:
        redis = await redis_client.get_client()
        key = f"{self.USER_SESSIONS_PREFIX}{email.lower()}"
        session_ids = await redis.smembers(key)
        count = 0
        for session_id in session_ids:
            await self.revoke_session(session_id)
            count += 1
        await redis.delete(key)
        return count

    async def list_user_sessions(self, email: str) -> list[dict]:
        """Hydrate every session_id in the user's session set into
        {session_id, created_at, last_rotated_at}, skipping any that expired
        out of Redis without a matching set removal."""
        redis = await redis_client.get_client()
        key = f"{self.USER_SESSIONS_PREFIX}{email.lower()}"
        session_ids = await redis.smembers(key)
        sessions: list[dict] = []
        for session_id in session_ids:
            session = await self.get_session(session_id)
            if not session:
                continue
            sessions.append(
                {
                    "session_id": session_id,
                    "created_at": session.get("created_at"),
                    "last_rotated_at": session.get("last_rotated_at"),
                }
            )
        return sessions

    async def rotate_refresh_token(self, refresh_token: str) -> Optional[tuple[str, str, str, str]]:
        """
        Validate refresh token and rotate it.
        Returns (session_id, email, role, new_refresh_token) or None.
        """
        refresh_hash = self._hash_token(refresh_token)
        redis = await redis_client.get_client()
        session_id = await redis.get(f"{self.REFRESH_PREFIX}{refresh_hash}")
        if not session_id:
            return None

        session = await self.get_session(session_id)
        if not session:
            await redis.delete(f"{self.REFRESH_PREFIX}{refresh_hash}")
            return None

        stored_hash = session.get("refresh_token_hash")
        if not secrets.compare_digest(stored_hash or "", refresh_hash):
            await self.revoke_session(session_id)
            return None

        new_refresh = secrets.token_urlsafe(48)
        new_hash = self._hash_token(new_refresh)
        session["refresh_token_hash"] = new_hash
        session["last_rotated_at"] = datetime.utcnow().isoformat()

        pipe = redis.pipeline()
        pipe.setex(
            f"{self.SESSION_PREFIX}{session_id}",
            self._refresh_ttl_seconds(),
            json.dumps(session),
        )
        pipe.delete(f"{self.REFRESH_PREFIX}{refresh_hash}")
        pipe.setex(
            f"{self.REFRESH_PREFIX}{new_hash}",
            self._refresh_ttl_seconds(),
            session_id,
        )
        await pipe.execute()

        return session_id, session["email"], session["role"], new_refresh

    async def get_session_id_for_refresh(self, refresh_token: str) -> Optional[str]:
        refresh_hash = self._hash_token(refresh_token)
        redis = await redis_client.get_client()
        return await redis.get(f"{self.REFRESH_PREFIX}{refresh_hash}")

    def build_auth_tokens(
        self,
        *,
        access_token: str,
        refresh_token: str,
        csrf_token: str,
        session_id: str,
    ) -> AuthTokens:
        return AuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            csrf_token=csrf_token,
            session_id=session_id,
            expires_in=self._access_ttl_seconds(),
        )
