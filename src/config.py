import sys

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional


from src.security.cors_policy import parse_cors_origins
from src.security.secrets_validation import collect_production_secret_errors


INSECURE_DEFAULTS = {
    "admin_password": "admin123",
    "jwt_secret": "chokmoki-jwt-secret-change-me",
}


class Settings(BaseSettings):
    # Environment
    environment: str = Field(default="development", env="ENVIRONMENT")

    # Mongo
    mongodb_uri: str = Field(..., env="MONGODB_URI")
    mongodb_db_name: str = Field(default="lowkey_ecom", env="MONGODB_DB_NAME")

    # Auth
    admin_email: str = Field(default="admin@chokmoki.com", env="ADMIN_EMAIL")
    admin_password: str = Field(default="admin123", env="ADMIN_PASSWORD")
    admin_password_hash: Optional[str] = Field(default=None, env="ADMIN_PASSWORD_HASH")
    jwt_secret: str = Field(default="chokmoki-jwt-secret-change-me", env="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", env="JWT_ALGORITHM")
    jwt_expiration_hours: int = Field(default=24, env="JWT_EXPIRATION_HOURS")
    jwt_access_ttl_minutes: int = Field(default=60, env="JWT_ACCESS_TTL_MINUTES")
    jwt_refresh_ttl_days: int = Field(default=7, env="JWT_REFRESH_TTL_DAYS")
    jwt_secret_previous: Optional[str] = Field(default=None, env="JWT_SECRET_PREVIOUS")
    admin_mfa_secret: Optional[str] = Field(default=None, env="ADMIN_MFA_SECRET")
    # Fernet key (base64 urlsafe, 32 raw bytes) used to encrypt per-admin TOTP
    # secrets at rest in the `admin_users` Mongo collection — generate with
    # `Fernet.generate_key()`. Required in production (see
    # secrets_validation.py); local/dev boots without it only because
    # validate_production_security() below is skipped outside production.
    admin_secret_encryption_key: Optional[str] = Field(
        default=None, env="ADMIN_SECRET_ENCRYPTION_KEY"
    )
    csrf_enabled: bool = Field(default=True, env="CSRF_ENABLED")
    admin_cookie_samesite: str = Field(default="lax", env="ADMIN_COOKIE_SAMESITE")
    admin_cookie_domain: Optional[str] = Field(default=None, env="ADMIN_COOKIE_DOMAIN")
    # Separate from admin_cookie_domain on purpose: the CSRF cookie is the
    # only one JS ever needs to read (httponly=False, by design, so the SPA
    # can echo it back as X-CSRF-Token). If the admin frontend and API live
    # on different hostnames (e.g. www.chokmoki.com vs api.chokmoki.com),
    # this MUST be a shared parent domain (".chokmoki.com") or
    # document.cookie on the frontend can never see it — cookies are
    # readable by JS only on their own exact hostname, regardless of
    # SameSite/CORS. The access/refresh cookies stay host-only (narrower,
    # safer) since they're httponly and only ever sent back to the API.
    admin_csrf_cookie_domain: Optional[str] = Field(default=None, env="ADMIN_CSRF_COOKIE_DOMAIN")
    admin_cookie_secure: Optional[bool] = Field(default=None, env="ADMIN_COOKIE_SECURE")
    admin_legacy_bearer_enabled: bool = Field(
        default=False, env="ADMIN_LEGACY_BEARER_ENABLED"
    )

    # Infra
    redis_url: str = Field(..., env="REDIS_URL")

    # CORS — comma-separated origins, e.g. https://shop.example.com,http://localhost:5173
    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173",
        env="CORS_ALLOWED_ORIGINS",
    )

    # Cron / internal
    cron_secret: Optional[str] = Field(default=None, env="CRON_SECRET")

    # Logging
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    # Directory for on-disk log files (rotated), mounted as a Docker volume
    # so logs survive container restarts/rebuilds and can be pulled off the
    # server independently of `docker logs` (which is not persistent).
    log_dir: str = Field(default="/app/logs", env="LOG_DIR")
    # Distinct filename per process (api vs worker) sharing the same mounted
    # volume — two OS processes rotating/appending the SAME file concurrently
    # would corrupt each other's file position/rotation state.
    log_file_name: str = Field(default="app.log", env="LOG_FILE_NAME")

    # Razorpay
    razorpay_key_id: str = Field(..., env="RAZORPAY_KEY_ID")
    razorpay_key_secret: str = Field(..., env="RAZORPAY_KEY_SECRET")
    razorpay_webhook_secret: Optional[str] = Field(default=None, env="RAZORPAY_WEBHOOK_SECRET")

    # Telegram
    telegram_enabled: bool = Field(default=False, env="TELEGRAM_ENABLED")
    telegram_bot_token: Optional[str] = Field(default=None, env="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = Field(default=None, env="TELEGRAM_CHAT_ID")
    telegram_product_base_url: str = Field(
        default="https://lowkey-ui.vercel.app/product", env="TELEGRAM_PRODUCT_BASE_URL"
    )

    # MSG91 — customer OTP login + lifecycle SMS notifications. Secrets/
    # toggle are env-backed (like Telegram); per-event template IDs are
    # NOT here — they're admin-editable rows in the `sms_templates` Mongo
    # collection (src/services/sms_template_service.py) so a new template
    # can be wired up from the admin panel without a redeploy.
    msg91_enabled: bool = Field(default=False, env="MSG91_ENABLED")
    msg91_auth_key: Optional[str] = Field(default=None, env="MSG91_AUTH_KEY")
    msg91_sender_id: Optional[str] = Field(default=None, env="MSG91_SENDER_ID")
    msg91_base_url: str = Field(default="https://control.msg91.com/api/v5", env="MSG91_BASE_URL")
    msg91_otp_expiry_seconds: int = Field(default=300, env="MSG91_OTP_EXPIRY_SECONDS")
    msg91_otp_length: int = Field(default=4, env="MSG91_OTP_LENGTH")

    # Brevo SMTP — email OTP login/signup (alternate to MSG91 while it's
    # unverified) plus order-confirmation/status-update emails. A plain SMTP
    # relay, not a special SDK — same "guarded, never raises" posture as
    # Msg91Service (src/services/email_service.py).
    smtp_host: str = Field(default="smtp-relay.brevo.com", env="SMTP_HOST")
    smtp_port: int = Field(default=587, env="SMTP_PORT")
    smtp_username: Optional[str] = Field(default=None, env="SMTP_USERNAME")
    smtp_password: Optional[str] = Field(default=None, env="SMTP_PASSWORD")
    smtp_from: Optional[str] = Field(default=None, env="SMTP_FROM")
    # Base URL of the deployed storefront — used to build absolute links
    # (order tracking, "view your order") inside outgoing emails.
    frontend_url: str = Field(default="https://www.chokmoki.com", env="FRONTEND_URL")
    email_otp_expiry_seconds: int = Field(default=300, env="EMAIL_OTP_EXPIRY_SECONDS")
    email_otp_length: int = Field(default=6, env="EMAIL_OTP_LENGTH")

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_username and self.smtp_password and self.smtp_from)

    # Customer auth (phone+OTP login) — separate token `type` claims from
    # admin ("customer_access"/"customer_refresh" vs "admin_access") so a
    # leaked customer token can never be replayed against an admin route,
    # even though both reuse the same JWT_SECRET/algorithm.
    customer_jwt_access_ttl_minutes: int = Field(
        default=60, env="CUSTOMER_JWT_ACCESS_TTL_MINUTES"
    )
    customer_jwt_refresh_ttl_days: int = Field(default=90, env="CUSTOMER_JWT_REFRESH_TTL_DAYS")

    @property
    def msg91_config(self) -> dict:
        """Non-secret shape exposed to admin routes/frontend."""
        return {
            "enabled": self.msg91_enabled,
            "sender_id": self.msg91_sender_id,
            "otp_expiry_seconds": self.msg91_otp_expiry_seconds,
            "otp_length": self.msg91_otp_length,
        }

    # Shiprocket
    shiprocket_enabled: bool = Field(default=False, env="SHIPROCKET_ENABLED")
    shiprocket_email: Optional[str] = Field(default=None, env="SHIPROCKET_EMAIL")
    shiprocket_password: Optional[str] = Field(default=None, env="SHIPROCKET_PASSWORD")
    # Must exactly match a pickup location name already configured in the
    # Shiprocket dashboard (Settings > Pickup Addresses) — required on every
    # order-create call.
    shiprocket_pickup_location: Optional[str] = Field(default=None, env="SHIPROCKET_PICKUP_LOCATION")
    # The pincode of that SAME pickup location — the order-create call takes
    # the location by name, but the courier-serviceability quote call needs
    # the actual pickup postcode, so both are required.
    shiprocket_pickup_pincode: Optional[str] = Field(default=None, env="SHIPROCKET_PICKUP_PINCODE")
    # Shared secret Shiprocket sends back as the `x-api-key` header on every
    # webhook call (configured in their dashboard under Settings > API >
    # Webhooks). Not an HMAC signature — a static token compare.
    shiprocket_webhook_token: Optional[str] = Field(default=None, env="SHIPROCKET_WEBHOOK_TOKEN")
    # Fallback package dimensions/weight when product data doesn't have them
    # (Product.weight_grams is frequently unset, and we don't collect
    # per-order package dimensions at checkout).
    shiprocket_default_weight_kg: float = Field(default=0.3, env="SHIPROCKET_DEFAULT_WEIGHT_KG")
    shiprocket_default_length_cm: float = Field(default=15.0, env="SHIPROCKET_DEFAULT_LENGTH_CM")
    shiprocket_default_breadth_cm: float = Field(default=10.0, env="SHIPROCKET_DEFAULT_BREADTH_CM")
    shiprocket_default_height_cm: float = Field(default=5.0, env="SHIPROCKET_DEFAULT_HEIGHT_CM")
    # Used only when the admin doesn't manually pick a courier on "Ready to Ship".
    shiprocket_default_courier_selection: str = Field(
        default="cheapest", env="SHIPROCKET_DEFAULT_COURIER_SELECTION"
    )

    # GST — tax display on invoices, NOT a price change. Storefront prices
    # are already GST-inclusive; these rates only tell Shiprocket how to
    # back-calculate the taxable value + tax split shown on the invoice it
    # generates (selling_price is documented "Inclusive of GST" in their
    # API). CGST/SGST vs IGST is Shiprocket's decision at invoice time,
    # based on pickup state vs place of supply — we only send the TOTAL
    # rate (cgst + sgst) per order item via the `tax` field; the split
    # config exists so the rate is expressed the same way it appears on the
    # invoice (1.5% + 1.5% = 3%, the rate for jewellery, HSN 7113).
    gst_enabled: bool = Field(default=True, env="GST_ENABLED")
    gst_cgst_percent: float = Field(default=1.5, env="GST_CGST_PERCENT")
    gst_sgst_percent: float = Field(default=1.5, env="GST_SGST_PERCENT")
    # Sterling silver jewellery = HSN 7113 (articles of jewellery of
    # precious metal). Confirm against your actual product classification —
    # imitation/fashion jewellery would be 7117 instead.
    gst_hsn_code: str = Field(default="7113", env="GST_HSN_CODE")

    @property
    def gst_total_percent(self) -> float:
        return self.gst_cgst_percent + self.gst_sgst_percent

    @property
    def gst_config(self) -> dict:
        """Shaped for API/frontend consumption: {generic: {cgst, sgst}}."""
        return {
            "enabled": self.gst_enabled,
            "generic": {
                "cgst": self.gst_cgst_percent,
                "sgst": self.gst_sgst_percent,
            },
            "total_percent": self.gst_total_percent,
            "hsn_code": self.gst_hsn_code,
        }

    # Invoice PDF generation (src/services/invoice_service.py) — the seller
    # ("Sold By") block printed on every generated document. Defaults match
    # the registered business details already configured in Shiprocket, so
    # our own invoices and Shiprocket's agree; override via env if any of
    # this changes. invoice_seller_state is what decides CGST+SGST
    # (customer in the same state) vs IGST (different state).
    invoice_brand_name: str = Field(default="Chokmoki", env="INVOICE_BRAND_NAME")
    invoice_brand_tagline: str = Field(
        default="Let the sparkle begin", env="INVOICE_BRAND_TAGLINE"
    )
    invoice_seller_name: str = Field(default="SUPRAR LLP", env="INVOICE_SELLER_NAME")
    invoice_seller_address1: str = Field(
        default="Shop No 12, 400/401 SV Road, 3rd Lane", env="INVOICE_SELLER_ADDRESS1"
    )
    invoice_seller_address2: str = Field(
        default="Lp-17/3/3/1 North 24 Parganas", env="INVOICE_SELLER_ADDRESS2"
    )
    invoice_seller_city: str = Field(default="North 24 Parganas", env="INVOICE_SELLER_CITY")
    invoice_seller_pincode: str = Field(default="700051", env="INVOICE_SELLER_PINCODE")
    invoice_seller_state: str = Field(default="West Bengal", env="INVOICE_SELLER_STATE")
    invoice_seller_state_code: str = Field(default="19", env="INVOICE_SELLER_STATE_CODE")
    invoice_seller_gstin: str = Field(default="19AETFS6652P1ZR", env="INVOICE_SELLER_GSTIN")
    invoice_seller_phone: str = Field(default="8981425898", env="INVOICE_SELLER_PHONE")
    invoice_seller_email: str = Field(
        default="sandip.tulsyan@gmail.com", env="INVOICE_SELLER_EMAIL"
    )
    invoice_number_prefix: str = Field(default="INV", env="INVOICE_NUMBER_PREFIX")
    invoice_logo_path: str = Field(default="assets/chokmoki_logo.jpg", env="INVOICE_LOGO_PATH")

    # R2 / S3
    r2_account_id: str = Field(default="", env="R2_ACCOUNT_ID")
    r2_access_key_id: str = Field(default="", env="R2_ACCESS_KEY_ID")
    r2_secret_access_key: str = Field(default="", env="R2_SECRET_ACCESS_KEY")
    r2_bucket: str = Field(default="chokmoki", env="R2_BUCKET")
    r2_key_prefix: str = Field(default="", env="R2_KEY_PREFIX")
    r2_public_base_url: str = Field(default="", env="R2_PUBLIC_BASE_URL")
    # Optional override for local/sandbox testing against an S3-compatible mock
    # (e.g. MinIO). Unset in production -> R2Service builds the real Cloudflare
    # R2 endpoint from r2_account_id as before.
    r2_endpoint_url: str = Field(default="", env="R2_ENDPOINT_URL")

    # Rate Limiting
    rate_limit_enabled: bool = Field(default=True, env="RATE_LIMIT_ENABLED")
    rate_limit_normal_get: int = Field(default=400, env="RATE_LIMIT_NORMAL_GET")
    rate_limit_normal_post: int = Field(default=400, env="RATE_LIMIT_NORMAL_POST")
    rate_limit_normal_time: str = Field(default="3m", env="RATE_LIMIT_NORMAL_TIME")
    rate_limit_order_max: int = Field(default=5, env="RATE_LIMIT_ORDER_MAX")
    rate_limit_order_time: str = Field(default="1h", env="RATE_LIMIT_ORDER_TIME")
    rate_limit_contact_max: int = Field(default=3, env="RATE_LIMIT_CONTACT_MAX")
    rate_limit_contact_time: str = Field(default="1h", env="RATE_LIMIT_CONTACT_TIME")
    rate_limit_newsletter_max: int = Field(default=3, env="RATE_LIMIT_NEWSLETTER_MAX")
    rate_limit_newsletter_time: str = Field(default="24h", env="RATE_LIMIT_NEWSLETTER_TIME")
    rate_limit_fail_closed: bool = Field(default=False, env="RATE_LIMIT_FAIL_CLOSED")
    rate_limit_auth_fail_closed: bool = Field(
        default=True, env="RATE_LIMIT_AUTH_FAIL_CLOSED"
    )
    rate_limit_config_file: Optional[str] = Field(default=None, env="RATE_LIMIT_CONFIG_FILE")
    trusted_proxy_enabled: bool = Field(default=False, env="TRUSTED_PROXY_ENABLED")
    trust_x_forwarded_for: bool = Field(default=False, env="TRUST_X_FORWARDED_FOR")
    rate_limit_ip_header: Optional[str] = Field(default=None, env="RATE_LIMIT_IP_HEADER")
    log_client_ip_headers: bool = Field(default=False, env="LOG_CLIENT_IP_HEADERS")

    # Login lockout
    login_max_failed_attempts: int = Field(default=5, env="LOGIN_MAX_FAILED_ATTEMPTS")
    login_lockout_seconds: int = Field(default=1800, env="LOGIN_LOCKOUT_SECONDS")
    login_failure_window_seconds: int = Field(
        default=900, env="LOGIN_FAILURE_WINDOW_SECONDS"
    )

    # Orders
    order_min_quantity: int = Field(default=1, env="ORDER_MIN_QUANTITY")
    order_max_quantity: int = Field(default=99, env="ORDER_MAX_QUANTITY")

    # Inventory
    inventory_enabled: bool = Field(default=True, env="INVENTORY_ENABLED")
    inventory_reservation_ttl_seconds: int = Field(
        default=3600, env="INVENTORY_RESERVATION_TTL_SECONDS"
    )

    # How often the worker process independently asks Razorpay for the true
    # status of anything still stuck in pending_order:* Redis keys — the
    # webhook-independent safety net for "payment succeeded but our webhook
    # never arrived/never got processed" (Razorpay outage, misconfigured
    # subscription, or an extended outage on our own webhook endpoint).
    # 30 min default — the bulk GET /v1/payments pass already recovers
    # everything with one API call per run regardless of interval, so this
    # only trades off "how long a missed webhook stays unrecovered" against
    # "how often we touch Razorpay's API at all"; 30 min is a comfortable
    # margin under Razorpay's rate limit even during an outage.
    payment_reconcile_interval_seconds: int = Field(
        default=1800, env="PAYMENT_RECONCILE_INTERVAL_SECONDS"
    )
    # How far back a reconciliation run looks for still-unresolved payment
    # attempts. Independent of inventory_reservation_ttl_seconds on purpose —
    # that TTL exists to release reserved stock, not to bound how long we're
    # willing to try recovering a missed webhook. A payment_attempts row
    # outlives the Redis pending_order key, so this can safely be longer.
    payment_reconcile_window_hours: int = Field(
        default=2, env="PAYMENT_RECONCILE_WINDOW_HOURS"
    )
    # After the bulk GET /v1/payments pass, anything still unresolved (bulk
    # fetch hit its page cap, or a payment settled right at the window edge)
    # gets a per-order fallback check via GET /v1/orders/{id}/payments — but
    # throttled, not all at once, so a large leftover set still can't spike
    # Razorpay API usage: this many calls, then sleep, repeat.
    payment_reconcile_fallback_batch_size: int = Field(
        default=3, env="PAYMENT_RECONCILE_FALLBACK_BATCH_SIZE"
    )
    payment_reconcile_fallback_interval_seconds: int = Field(
        default=60, env="PAYMENT_RECONCILE_FALLBACK_INTERVAL_SECONDS"
    )

    # Fraud Detection
    fraud_enabled: bool = Field(default=False, env="FRAUD_ENABLED")
    fraud_fail_closed: bool = Field(default=False, env="FRAUD_FAIL_CLOSED")
    fraud_rules_file: str = Field(
        default="config/fraud_rules.yaml", env="FRAUD_RULES_FILE"
    )
    fraud_audit_enabled: bool = Field(default=True, env="FRAUD_AUDIT_ENABLED")
    fraud_velocity_window_seconds: int = Field(
        default=3600, env="FRAUD_VELOCITY_WINDOW_SECONDS"
    )
    fraud_duplicate_window_seconds: int = Field(
        default=900, env="FRAUD_DUPLICATE_WINDOW_SECONDS"
    )
    fraud_velocity_email_threshold: int = Field(
        default=5, env="FRAUD_VELOCITY_EMAIL_THRESHOLD"
    )
    fraud_velocity_ip_threshold: int = Field(default=10, env="FRAUD_VELOCITY_IP_THRESHOLD")

    # Stock-level Telegram alerts (src/services/stock_alerts.py) — a product's
    # per-region qty crossing at/below this value fires a "low stock" alert
    # (crossing to 0 fires "out of stock" instead, not in addition).
    low_stock_threshold: int = Field(default=10, env="LOW_STOCK_THRESHOLD")

    # Multi-region pricing / GeoIP
    geoip_service_url: Optional[str] = Field(default=None, env="GEOIP_SERVICE_URL")
    geoip_lookup_timeout_seconds: float = Field(
        default=3.0, env="GEOIP_LOOKUP_TIMEOUT_SECONDS"
    )
    # How long an IP -> country lookup is cached in Redis. Keeps repeated
    # requests from the same visitor (page loads, product fetches) from
    # hammering the geoip-discovery service — one lookup per IP per window.
    geoip_cache_ttl_seconds: int = Field(
        default=14400, env="GEOIP_CACHE_TTL_SECONDS"  # 4 hours
    )
    supported_market_countries: str = Field(
        default="IN,AU,NZ", env="SUPPORTED_MARKET_COUNTRIES"
    )
    # Markets that can pay online today (Razorpay only supports INR). COD
    # has no such gate — it needs no payment gateway, so it's available
    # from every market. AU/NZ/default are priceable, browsable, and can
    # check out via COD; only the "pay online now" option is India-only
    # until a gateway that handles AUD/NZD is wired up — see
    # order_service.py's _validate_and_prepare_order.
    prepaid_enabled_countries: str = Field(
        default="IN", env="PREPAID_ENABLED_COUNTRIES"
    )

    # Idempotency
    idempotency_enabled: bool = Field(default=True, env="IDEMPOTENCY_ENABLED")
    idempotency_ttl_seconds: int = Field(default=86400, env="IDEMPOTENCY_TTL_SECONDS")
    idempotency_required_in_production: bool = Field(
        default=True, env="IDEMPOTENCY_REQUIRED_IN_PRODUCTION"
    )

    # Metrics
    metrics_enabled: bool = Field(default=True, env="METRICS_ENABLED")
    metrics_token: Optional[str] = Field(default=None, env="METRICS_TOKEN")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
    )

    @field_validator("environment")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        return (value or "development").strip().lower()

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def admin_password_configured(self) -> bool:
        return bool(
            (self.admin_password_hash and self.admin_password_hash.strip())
            or (self.admin_password and self.admin_password.strip())
        )

    @property
    def admin_mfa_enabled(self) -> bool:
        return bool(self.admin_mfa_secret and self.admin_mfa_secret.strip())

    @property
    def cookie_secure(self) -> bool:
        if self.admin_cookie_secure is not None:
            return self.admin_cookie_secure
        return self.is_production

    @property
    def cookie_samesite(self) -> str:
        value = (self.admin_cookie_samesite or "lax").strip().lower()
        if value not in {"lax", "strict", "none"}:
            return "lax"
        return value

    @property
    def cors_origins_list(self) -> List[str]:
        allow_wildcard = not self.is_production
        return parse_cors_origins(
            self.cors_allowed_origins or "",
            allow_wildcard=allow_wildcard,
        )

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        try:
            origins = parse_cors_origins(
                self.cors_allowed_origins or "",
                allow_wildcard=not self.is_production,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        if not self.is_production:
            # Every strength/known-weak-secret check below is SKIPPED
            # whenever ENVIRONMENT isn't exactly "production" — correct for
            # genuine local dev, but a real deployment that simply forgot to
            # set ENVIRONMENT=production would silently boot with insecure
            # defaults (admin123, the placeholder JWT secret, etc.) and no
            # error at all. Can't reliably tell "forgotten env var" apart
            # from "genuine local dev" from in here, so: print a loud,
            # impossible-to-miss warning on every non-production boot rather
            # than silently proceeding either way. Plain print (not the
            # app logger) — src/plugins/logger.py imports this module, so
            # importing it here would be circular, and this needs to run
            # before the logger even exists regardless.
            print(
                f"\n{'!' * 70}\n"
                f"WARNING: ENVIRONMENT='{self.environment}', not 'production' — "
                f"production security validation (secret strength, CORS, "
                f"rate-limit fail-closed, etc.) is SKIPPED.\n"
                f"If this is a real deployment, this is very likely a "
                f"misconfiguration: set ENVIRONMENT=production.\n"
                f"{'!' * 70}\n",
                file=sys.stderr,
            )
            return self

        errors: List[str] = collect_production_secret_errors(
            admin_password=self.admin_password,
            admin_password_hash=self.admin_password_hash,
            jwt_secret=self.jwt_secret,
            jwt_secret_previous=self.jwt_secret_previous,
            cron_secret=self.cron_secret,
            metrics_enabled=self.metrics_enabled,
            metrics_token=self.metrics_token,
            r2_access_key_id=self.r2_access_key_id,
            r2_secret_access_key=self.r2_secret_access_key,
            razorpay_webhook_secret=self.razorpay_webhook_secret,
            admin_secret_encryption_key=self.admin_secret_encryption_key,
        )

        if not origins:
            errors.append(
                "CORS_ALLOWED_ORIGINS must list explicit storefront origins in production"
            )

        if self.order_min_quantity < 1:
            errors.append("ORDER_MIN_QUANTITY must be >= 1")

        if self.order_max_quantity < self.order_min_quantity:
            errors.append("ORDER_MAX_QUANTITY must be >= ORDER_MIN_QUANTITY")

        if not self.rate_limit_auth_fail_closed:
            errors.append("RATE_LIMIT_AUTH_FAIL_CLOSED must be true in production")

        if self.trust_x_forwarded_for:
            errors.append("TRUST_X_FORWARDED_FOR must be false in production")

        if self.admin_legacy_bearer_enabled:
            errors.append("ADMIN_LEGACY_BEARER_ENABLED must be false in production")

        if not self.fraud_enabled:
            errors.append("FRAUD_ENABLED must be true in production")

        if not self.idempotency_enabled:
            errors.append("IDEMPOTENCY_ENABLED must be true in production")

        if errors:
            raise ValueError(
                "Insecure production configuration:\n- " + "\n- ".join(errors)
            )

        return self


def load_settings() -> Settings:
    """Load settings and fail fast on insecure production configuration."""
    return Settings()


settings = load_settings()
