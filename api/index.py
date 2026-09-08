"""App entrypoint: FastAPI construction, middleware, lifespan, router wiring.

Route handlers live in api/routes/*.py (one module per domain, mirroring the
src/services/ layout); optional/app-specific imports live in api/bootstrap.py
so a broken import can't crash the whole app at boot.
"""
from contextlib import asynccontextmanager
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv(override=False)

from api.bootstrap import (
    AdminAuditMiddleware,
    CorrelationIdMiddleware,
    OrderService,
    R2Service,
    RateLimitMiddleware,
    db,
    logger,
    redis_client,
    register_exception_handlers,
    settings,
)


def _cors_middleware_kwargs():
    from src.security.cors_policy import build_cors_middleware_kwargs

    origins = settings.cors_origins_list if settings else ["http://localhost:5173"]
    return build_cors_middleware_kwargs(origins)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage database, Redis, and background-task lifecycle."""
    try:
        if db:
            await db.connect()
            if OrderService is not None:
                try:
                    await OrderService().ensure_indexes()
                except Exception as idx_err:
                    if logger:
                        logger.warning(f"Order index setup skipped: {idx_err}")
            try:
                from src.services.payment_reconciliation_service import PaymentReconciliationService

                await PaymentReconciliationService().ensure_indexes()
            except Exception as idx_err:
                if logger:
                    logger.warning(f"Payment reconcile index setup skipped: {idx_err}")
            try:
                from src.services.inventory_service import InventoryService

                await InventoryService().ensure_indexes()
            except Exception as idx_err:
                if logger:
                    logger.warning(f"Inventory reservation index setup skipped: {idx_err}")
            try:
                from src.services.user_service import UserService
                from src.services.sms_template_service import SmsTemplateService

                await UserService().ensure_indexes()
                await SmsTemplateService().ensure_indexes()
                await SmsTemplateService().seed_defaults()
            except Exception as idx_err:
                if logger:
                    logger.warning(f"Customer/SMS index setup skipped: {idx_err}")
            try:
                from src.services.discount_service import CouponService

                await CouponService().ensure_indexes()
            except Exception as idx_err:
                if logger:
                    logger.warning(f"Coupon index setup skipped: {idx_err}")
        if redis_client:
            await redis_client.connect()
        # Ensure the R2 media bucket exists for dynamic asset hosting
        if R2Service is not None:
            try:
                R2Service().ensure_bucket()
            except Exception as r2_err:
                if logger:
                    logger.warning(f"R2 bucket check skipped: {r2_err}")
    except Exception as e:
        if logger:
            logger.error(f"Startup connection error: {e}")
        print(f"Startup connection error: {e}", file=sys.stderr)

    # Background stream consumers (Telegram alerts, crash-safe Razorpay
    # payment confirmation) run in the SEPARATE `worker` process/container
    # (src/worker.py, `python -m src.worker`) — not here. This process only
    # publishes events (XADD, sub-millisecond) and serves requests; it no
    # longer runs any consumer, so an API restart/crash can never drop an
    # in-flight payment confirmation.
    yield

    try:
        if db:
            await db.close()
        if redis_client:
            await redis_client.close()
    except Exception as e:
        if logger:
            logger.error(f"Shutdown connection error: {e}")
        print(f"Shutdown connection error: {e}", file=sys.stderr)


from src.security.openapi import fastapi_docs_kwargs

# Export THIS ONLY
app = FastAPI(
    lifespan=lifespan,
    **fastapi_docs_kwargs(bool(settings and settings.is_production)),
)

if logger is not None:
    register_exception_handlers(app, logger)

if RateLimitMiddleware:
    app.add_middleware(RateLimitMiddleware)

if AdminAuditMiddleware:
    app.add_middleware(AdminAuditMiddleware)

app.add_middleware(CORSMiddleware, **_cors_middleware_kwargs())

if CorrelationIdMiddleware:
    app.add_middleware(CorrelationIdMiddleware)

from src.middleware.metrics_middleware import MetricsMiddleware

app.add_middleware(MetricsMiddleware)

from src.middleware.security_headers import SecurityHeadersMiddleware

app.add_middleware(SecurityHeadersMiddleware)


from api.routes import (
    account,
    admin_auth,
    admin_backup,
    admin_catalog,
    admin_content,
    admin_coupons,
    admin_fraud,
    admin_import,
    admin_inbox,
    admin_orders,
    admin_orders_backup,
    admin_reconciliation,
    admin_sms,
    admin_upload,
    admin_users,
    auth,
    contact,
    coupons,
    cqrs,
    cron,
    geo,
    health,
    media,
    orders,
    pincode,
    storefront,
)

app.include_router(health.router)
app.include_router(storefront.router)
app.include_router(pincode.router)
app.include_router(geo.router)
app.include_router(contact.router)
app.include_router(orders.router)
app.include_router(coupons.router)
app.include_router(auth.router)
app.include_router(account.router)
app.include_router(admin_auth.router)
app.include_router(admin_upload.router)
app.include_router(admin_import.router)
app.include_router(admin_backup.router)
app.include_router(admin_orders_backup.router)
app.include_router(admin_orders.router)
app.include_router(admin_catalog.router)
app.include_router(admin_coupons.router)
app.include_router(admin_content.router)
app.include_router(admin_inbox.router)
app.include_router(admin_fraud.router)
app.include_router(admin_reconciliation.router)
app.include_router(admin_sms.router)
app.include_router(admin_users.router)
app.include_router(media.router)
app.include_router(cron.router)
app.include_router(cqrs.router)
