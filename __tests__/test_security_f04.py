"""F-04 secrets hardening tests."""

from __future__ import annotations

import os
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import INSECURE_DEFAULTS, Settings
from src.security.password import hash_password, verify_admin_password
from src.security.secrets_validation import (
    collect_production_secret_errors,
    estimate_entropy_bits,
    is_known_weak_secret,
    validate_jwt_secret,
)


STRONG_JWT_SECRET = "v9K!mQ2@nP7#xR4$wL8%zT1^yU6&hJ3*"
STRONG_CRON_SECRET = "cron-secret-rotation-32chars!"
STRONG_METRICS_TOKEN = "metrics-token-rotation-32ch!"
STRONG_ADMIN_PASSWORD = "production-admin-pass-99!"
STRONG_ADMIN_HASH = hash_password(STRONG_ADMIN_PASSWORD)
STRONG_ADMIN_SECRET_ENCRYPTION_KEY = Fernet.generate_key().decode()


def _base_production_env(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_example")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "razorpay-live-secret-32chars!")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://shop.example.com")
    monkeypatch.setenv("JWT_SECRET", STRONG_JWT_SECRET)
    monkeypatch.setenv("CRON_SECRET", STRONG_CRON_SECRET)
    monkeypatch.setenv("METRICS_TOKEN", STRONG_METRICS_TOKEN)
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", STRONG_ADMIN_HASH)
    monkeypatch.setenv("ADMIN_SECRET_ENCRYPTION_KEY", STRONG_ADMIN_SECRET_ENCRYPTION_KEY)
    monkeypatch.setenv("FRAUD_ENABLED", "true")
    monkeypatch.setenv("IDEMPOTENCY_ENABLED", "true")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)


class TestSecretsValidation:
    def test_known_repo_jwt_secret_is_blocked(self):
        assert is_known_weak_secret("chokmoki-super-secret-jwt-key-change-in-prod")

    def test_strong_jwt_passes_validation(self):
        assert validate_jwt_secret(STRONG_JWT_SECRET) == []

    def test_low_entropy_jwt_rejected(self):
        errors = validate_jwt_secret("a" * 40)
        assert any("entropy" in error.lower() for error in errors)

    def test_entropy_helper_returns_positive_bits(self):
        assert estimate_entropy_bits(STRONG_JWT_SECRET) >= 128


class TestPasswordHashing:
    def test_bcrypt_hash_verifies(self):
        hashed = hash_password("local-dev-password-12")
        assert verify_admin_password(
            "local-dev-password-12",
            password_hash=hashed,
            fallback_plaintext=None,
        )

    def test_plaintext_fallback_for_development(self):
        assert verify_admin_password(
            "admin123",
            password_hash=None,
            fallback_plaintext="admin123",
        )


class TestProductionConfigGuard:
    def test_development_allows_defaults(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
        monkeypatch.setenv("ADMIN_PASSWORD", INSECURE_DEFAULTS["admin_password"])
        monkeypatch.setenv("JWT_SECRET", INSECURE_DEFAULTS["jwt_secret"])
        settings = Settings()
        assert settings.admin_password == INSECURE_DEFAULTS["admin_password"]

    def test_production_rejects_default_jwt_secret(self, monkeypatch):
        _base_production_env(monkeypatch)
        monkeypatch.setenv("JWT_SECRET", INSECURE_DEFAULTS["jwt_secret"])

        with pytest.raises(ValueError, match="Insecure production configuration"):
            Settings()

    def test_production_rejects_repo_known_jwt_secret(self, monkeypatch):
        _base_production_env(monkeypatch)
        monkeypatch.setenv(
            "JWT_SECRET", "chokmoki-super-secret-jwt-key-change-in-prod"
        )

        with pytest.raises(ValueError, match="known or documented default"):
            Settings()

    def test_production_rejects_plaintext_admin_password_without_hash(
        self, monkeypatch
    ):
        _base_production_env(monkeypatch)
        monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
        monkeypatch.setenv("ADMIN_PASSWORD", STRONG_ADMIN_PASSWORD)

        with pytest.raises(ValueError, match="ADMIN_PASSWORD_HASH is required"):
            Settings(_env_file=None)

    def test_production_rejects_default_admin_password(self, monkeypatch):
        _base_production_env(monkeypatch)
        monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
        monkeypatch.setenv("ADMIN_PASSWORD", INSECURE_DEFAULTS["admin_password"])

        with pytest.raises(ValueError, match="Insecure production configuration"):
            Settings()

    def test_production_accepts_hashed_admin_credentials(self, monkeypatch):
        _base_production_env(monkeypatch)
        settings = Settings(_env_file=None)
        assert settings.admin_password_hash == STRONG_ADMIN_HASH

    def test_production_rejects_plaintext_when_hash_configured(self, monkeypatch):
        _base_production_env(monkeypatch)
        monkeypatch.setenv("ADMIN_PASSWORD", STRONG_ADMIN_PASSWORD)

        with pytest.raises(ValueError, match="Do not set plaintext ADMIN_PASSWORD"):
            Settings()

    def test_production_requires_metrics_token_when_enabled(self, monkeypatch):
        _base_production_env(monkeypatch)
        monkeypatch.delenv("METRICS_TOKEN", raising=False)

        with pytest.raises(ValueError, match="METRICS_TOKEN"):
            Settings(_env_file=None)

    def test_production_requires_admin_secret_encryption_key(self, monkeypatch):
        _base_production_env(monkeypatch)
        monkeypatch.delenv("ADMIN_SECRET_ENCRYPTION_KEY", raising=False)

        with pytest.raises(ValueError, match="ADMIN_SECRET_ENCRYPTION_KEY must be set"):
            Settings(_env_file=None)

    def test_production_rejects_malformed_admin_secret_encryption_key(self, monkeypatch):
        _base_production_env(monkeypatch)
        monkeypatch.setenv("ADMIN_SECRET_ENCRYPTION_KEY", "not-a-real-fernet-key")

        with pytest.raises(ValueError, match="ADMIN_SECRET_ENCRYPTION_KEY must be a valid Fernet key"):
            Settings(_env_file=None)

    def test_production_accepts_valid_admin_secret_encryption_key(self, monkeypatch):
        _base_production_env(monkeypatch)
        settings = Settings(_env_file=None)
        assert settings.admin_secret_encryption_key == STRONG_ADMIN_SECRET_ENCRYPTION_KEY

    def test_jwt_rotation_secret_is_validated(self, monkeypatch):
        _base_production_env(monkeypatch)
        monkeypatch.setenv("JWT_SECRET_PREVIOUS", INSECURE_DEFAULTS["jwt_secret"])

        with pytest.raises(ValueError, match="JWT_SECRET_PREVIOUS"):
            Settings()


class TestProductionSecretCollector:
    def test_r2_credentials_must_be_paired(self):
        errors = collect_production_secret_errors(
            admin_password="",
            admin_password_hash=STRONG_ADMIN_HASH,
            jwt_secret=STRONG_JWT_SECRET,
            jwt_secret_previous=None,
            cron_secret=STRONG_CRON_SECRET,
            metrics_enabled=True,
            metrics_token=STRONG_METRICS_TOKEN,
            r2_access_key_id="abc123",
            r2_secret_access_key="",
            razorpay_webhook_secret=None,
        )
        assert any("R2_ACCESS_KEY_ID" in error for error in errors)


class TestAdminAuthHashedPassword:
    @pytest.mark.asyncio
    async def test_authenticate_accepts_bcrypt_hash(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
        monkeypatch.setenv("ADMIN_EMAIL", "admin@test.com")
        monkeypatch.setenv("ADMIN_PASSWORD_HASH", STRONG_ADMIN_HASH)
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

        from unittest.mock import AsyncMock, patch

        from src.services.admin_auth_service import AdminAuthService

        service = AdminAuthService()
        with patch.object(
            service.lockout, "assert_not_locked", new_callable=AsyncMock
        ), patch.object(
            service.lockout, "record_success", new_callable=AsyncMock
        ), patch.object(
            service.sessions, "create_session", new_callable=AsyncMock
        ) as mock_create:
            mock_create.return_value = ("session-1", "refresh-token", "csrf-token")
            with patch("src.services.admin_auth_service.settings") as mock_settings:
                mock_settings.admin_email = "admin@test.com"
                mock_settings.admin_password_configured = True
                mock_settings.admin_password_hash = STRONG_ADMIN_HASH
                mock_settings.admin_password = ""
                mock_settings.admin_mfa_enabled = False
                mock_settings.jwt_access_ttl_minutes = 60
                mock_settings.jwt_secret = STRONG_JWT_SECRET
                mock_settings.jwt_algorithm = "HS256"

                result = await service.authenticate(
                    "admin@test.com", STRONG_ADMIN_PASSWORD
                )

        assert result is not None
        assert result.email == "admin@test.com"
