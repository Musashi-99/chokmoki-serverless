"""Encrypts/decrypts per-admin TOTP secrets at rest.

`cryptography` is already a transitive dependency via
`python-jose[cryptography]` (requirements.txt), so no new package is added
for this. Key comes from ADMIN_SECRET_ENCRYPTION_KEY (src/config.py),
required in production (src/security/secrets_validation.py).
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from src.config import settings


class TotpSecretEncryptionNotConfigured(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = (settings.admin_secret_encryption_key or "").strip()
    if not key:
        raise TotpSecretEncryptionNotConfigured(
            "ADMIN_SECRET_ENCRYPTION_KEY is not configured"
        )
    return Fernet(key.encode("utf-8"))


def encrypt_totp_secret(plain_secret: str) -> str:
    return _fernet().encrypt(plain_secret.encode("utf-8")).decode("utf-8")


def decrypt_totp_secret(encrypted_secret: str) -> str:
    try:
        return _fernet().decrypt(encrypted_secret.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Stored TOTP secret could not be decrypted") from exc
