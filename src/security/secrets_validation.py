"""Startup secret strength validation and known-weak secret rejection."""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from typing import Iterable, List, Optional

BCRYPT_HASH_PATTERN = re.compile(r"^\$2[aby]\$\d{2}\$.{53}$")

KNOWN_WEAK_SECRETS = frozenset(
    {
        "admin123",
        "password",
        "password123",
        "changeme",
        "secret",
        "test",
        "dummy_secret",
        "chokmoki-jwt-secret-change-me",
        "chokmoki-super-secret-jwt-key-change-in-prod",
    }
)

MIN_JWT_SECRET_LENGTH = 32
MIN_JWT_ENTROPY_BITS = 128
MIN_PASSWORD_ENTROPY_BITS = 60
MIN_GENERIC_SECRET_LENGTH = 16
MIN_GENERIC_ENTROPY_BITS = 80


def normalize_secret(value: str) -> str:
    return (value or "").strip()


def is_known_weak_secret(value: str) -> bool:
    normalized = normalize_secret(value).lower()
    if not normalized:
        return True
    return normalized in KNOWN_WEAK_SECRETS


def estimate_entropy_bits(value: str) -> float:
    text = normalize_secret(value)
    if not text:
        return 0.0

    length = len(text)
    counts = Counter(text)
    shannon = -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )
    return shannon * length


def validate_bcrypt_hash(value: str, *, field_name: str) -> List[str]:
    errors: List[str] = []
    normalized = normalize_secret(value)
    if not normalized:
        errors.append(f"{field_name} must be set")
        return errors
    if not BCRYPT_HASH_PATTERN.match(normalized):
        errors.append(f"{field_name} must be a valid bcrypt hash")
    return errors


def validate_jwt_secret(value: str, *, field_name: str = "JWT_SECRET") -> List[str]:
    errors: List[str] = []
    normalized = normalize_secret(value)

    if len(normalized) < MIN_JWT_SECRET_LENGTH:
        errors.append(f"{field_name} must be at least {MIN_JWT_SECRET_LENGTH} characters")

    if is_known_weak_secret(normalized):
        errors.append(f"{field_name} must not use a known or documented default value")

    if estimate_entropy_bits(normalized) < MIN_JWT_ENTROPY_BITS:
        errors.append(
            f"{field_name} does not meet minimum entropy requirements "
            f"({MIN_JWT_ENTROPY_BITS} bits)"
        )

    return errors


def validate_admin_password_plaintext(
    value: str, *, field_name: str = "ADMIN_PASSWORD"
) -> List[str]:
    errors: List[str] = []
    normalized = normalize_secret(value)

    if len(normalized) < 12:
        errors.append(f"{field_name} must be at least 12 characters")

    if is_known_weak_secret(normalized):
        errors.append(f"{field_name} must not use a known or documented default value")

    if estimate_entropy_bits(normalized) < MIN_PASSWORD_ENTROPY_BITS:
        errors.append(
            f"{field_name} does not meet minimum entropy requirements "
            f"({MIN_PASSWORD_ENTROPY_BITS} bits)"
        )

    return errors


def validate_generic_secret(
    value: Optional[str],
    *,
    field_name: str,
    min_length: int = MIN_GENERIC_SECRET_LENGTH,
    min_entropy_bits: float = MIN_GENERIC_ENTROPY_BITS,
    required: bool = True,
) -> List[str]:
    errors: List[str] = []
    normalized = normalize_secret(value or "")

    if not normalized:
        if required:
            errors.append(f"{field_name} must be set")
        return errors

    if len(normalized) < min_length:
        errors.append(f"{field_name} must be at least {min_length} characters")

    if is_known_weak_secret(normalized):
        errors.append(f"{field_name} must not use a known or documented default value")

    if estimate_entropy_bits(normalized) < min_entropy_bits:
        errors.append(
            f"{field_name} does not meet minimum entropy requirements "
            f"({min_entropy_bits} bits)"
        )

    return errors


def validate_optional_jwt_secret(
    value: Optional[str], *, field_name: str
) -> List[str]:
    normalized = normalize_secret(value or "")
    if not normalized:
        return []
    return validate_jwt_secret(normalized, field_name=field_name)


def validate_fernet_key(value: Optional[str], *, field_name: str) -> List[str]:
    errors: List[str] = []
    normalized = normalize_secret(value or "")
    if not normalized:
        errors.append(f"{field_name} must be set")
        return errors
    try:
        from cryptography.fernet import Fernet

        Fernet(normalized.encode("utf-8"))
    except Exception:
        errors.append(f"{field_name} must be a valid Fernet key (Fernet.generate_key())")
    return errors


def validate_r2_credentials(
    access_key_id: str, secret_access_key: str
) -> List[str]:
    errors: List[str] = []
    key_id = normalize_secret(access_key_id)
    secret_key = normalize_secret(secret_access_key)

    if bool(key_id) != bool(secret_key):
        errors.append("R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY must both be set or both empty")

    if key_id and is_known_weak_secret(key_id):
        errors.append("R2_ACCESS_KEY_ID must not use a known or placeholder value")

    if secret_key and (
        is_known_weak_secret(secret_key)
        or estimate_entropy_bits(secret_key) < MIN_GENERIC_ENTROPY_BITS
    ):
        errors.append("R2_SECRET_ACCESS_KEY must not use a known or weak value")

    return errors


def collect_production_secret_errors(
  *,
  admin_password: str,
  admin_password_hash: Optional[str],
  jwt_secret: str,
  jwt_secret_previous: Optional[str],
  cron_secret: Optional[str],
  metrics_enabled: bool,
  metrics_token: Optional[str],
  r2_access_key_id: str,
  r2_secret_access_key: str,
  razorpay_webhook_secret: Optional[str],
  admin_secret_encryption_key: Optional[str] = None,
) -> List[str]:
    errors: List[str] = []

    if admin_password_hash:
        errors.extend(validate_bcrypt_hash(admin_password_hash, field_name="ADMIN_PASSWORD_HASH"))
        if os.environ.get("ADMIN_PASSWORD", "").strip():
            errors.append(
                "Do not set plaintext ADMIN_PASSWORD when ADMIN_PASSWORD_HASH is configured"
            )
    else:
        errors.append("ADMIN_PASSWORD_HASH is required in production")
        errors.extend(
            validate_admin_password_plaintext(admin_password, field_name="ADMIN_PASSWORD")
        )

    errors.extend(validate_jwt_secret(jwt_secret))
    errors.extend(
        validate_optional_jwt_secret(
            jwt_secret_previous, field_name="JWT_SECRET_PREVIOUS"
        )
    )
    errors.extend(
        validate_generic_secret(cron_secret, field_name="CRON_SECRET", required=True)
    )

    if metrics_enabled:
        errors.extend(
            validate_generic_secret(
                metrics_token,
                field_name="METRICS_TOKEN",
                required=True,
            )
        )

    errors.extend(validate_r2_credentials(r2_access_key_id, r2_secret_access_key))

    errors.extend(
        validate_fernet_key(
            admin_secret_encryption_key, field_name="ADMIN_SECRET_ENCRYPTION_KEY"
        )
    )

    if normalize_secret(razorpay_webhook_secret):
        errors.extend(
            validate_generic_secret(
                razorpay_webhook_secret,
                field_name="RAZORPAY_WEBHOOK_SECRET",
                required=True,
            )
        )

    return _dedupe(errors)


def _dedupe(errors: Iterable[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for error in errors:
        if error not in seen:
            seen.add(error)
            ordered.append(error)
    return ordered
