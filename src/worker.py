"""Standalone worker process: runs the background stream consumers
(Telegram alerts + crash-safe Razorpay payment confirmation) in a SEPARATE
OS process/container from the API — not as asyncio tasks inside the request-
serving FastAPI process.

Why: those consumers used to run inside api/index.py's lifespan. A crash,
restart, or resource spike in the request-serving process took payment
processing down with it, and a stuck consumer competed with the API for the
same process's CPU/memory. This process shares the same Redis (transport)
and MongoDB (state) as the API — nothing else changes; XADD from the API,
XREADGROUP from here, exactly what Redis Streams is designed for.

Run via: python -m src.worker
"""
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=False)

from src.config import settings
from src.database.connection import db
from src.database.redis_connection import redis_client
from src.plugins.logger import logger
from src.services.system_log_service import SystemLogService
from src.alerts.consumer import AlertConsumer
from src.orders.consumer import OrderEventConsumer

# Written to on every heartbeat tick; the Docker healthcheck (docker-compose.yml)
# just checks this file's mtime is recent. This process isn't an HTTP server,
# so it can't reuse the API's /health/ready — a freshness-checked file in the
# same mounted volume is the simplest equivalent that needs no new dependency.
HEARTBEAT_FILE = Path("/app/logs/worker.heartbeat")
HEARTBEAT_INTERVAL_SECONDS = 15


async def reconcile_loop() -> None:
    """Webhook-independent safety net: every `payment_reconcile_interval_seconds`,
    asks Razorpay directly for the true status of any payment_attempts record
    still 'pending' within the last `payment_reconcile_window_hours` — recovering
    orders whose webhook never arrived. Runs here (not system cron — not even
    installed on the deploy box) since this process is already a persistent,
    long-running worker. A single failed run must never kill the loop — just
    log and retry next interval. See src/services/payment_reconciliation_service.py.
    """
    from src.services.payment_reconciliation_service import PaymentReconciliationService

    interval = settings.payment_reconcile_interval_seconds
    service = PaymentReconciliationService()
    while True:
        await asyncio.sleep(interval)
        try:
            await service.run()
        except Exception as e:
            if logger:
                logger.error(f"Payment reconcile loop failed: {e}")


async def facebook_catalog_loop() -> None:
    """Resync every product an admin has opted into the Meta catalog, so
    price/stock/availability there never drifts from the site (an order
    decrementing stock never passes through the admin edit hook). A failed
    run is logged and retried next interval — it must never kill the loop."""
    from src.services.facebook_catalog_service import FacebookCatalogService
    from src.services.product_service import ProductService

    interval = settings.fb_catalog_reconcile_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            products = await ProductService().list_facebook_synced()
            if products:
                sent = await FacebookCatalogService().sync_products(products)
                if logger:
                    logger.info(f"Facebook catalog reconcile: resynced {sent} product(s)")
        except Exception as e:
            if logger:
                logger.error(f"Facebook catalog reconcile failed: {e}")


async def heartbeat_loop() -> None:
    """Proves the worker's event loop is alive and scheduling tasks, not just
    that the process exists — a wedged event loop (e.g. something blocking
    it without ever yielding) would stop this file from updating even though
    the OS process is still running. Deliberately supervised like every
    other task below: if writing the heartbeat itself starts failing (e.g.
    disk full, volume unmounted), that's worth crashing loudly for too.
    """
    while True:
        HEARTBEAT_FILE.write_text(str(asyncio.get_running_loop().time()))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def main() -> None:
    await db.connect()
    await redis_client.connect()

    from src.services.payment_reconciliation_service import PaymentReconciliationService
    from src.services.inventory_service import InventoryService
    await PaymentReconciliationService().ensure_indexes()
    await InventoryService().ensure_indexes()

    tasks: list[asyncio.Task] = []
    # This consumer drives every customer notification (order-confirmation
    # and status-update emails via Brevo, lifecycle SMS via MSG91) as well
    # as internal Telegram alerts — it must run whenever ANY of those
    # channels is enabled, not just Telegram. Previously gated on
    # telegram_enabled alone, which meant every other channel silently
    # never fired at all with Telegram off (each handler already no-ops
    # gracefully for its own disabled channel, so running this unconditionally
    # is safe even with everything disabled).
    if settings.telegram_enabled or settings.msg91_enabled or settings.email_enabled:
        tasks.append(asyncio.create_task(AlertConsumer().run(), name="alert_consumer"))
    else:
        if logger:
            logger.info(
                "Worker: no notification channel enabled (Telegram/MSG91/email all off), "
                "skipping AlertConsumer"
            )

    # Unconditional — core payment processing, not an optional notification.
    tasks.append(asyncio.create_task(OrderEventConsumer().run(), name="order_event_consumer"))
    tasks.append(asyncio.create_task(reconcile_loop(), name="payment_reconcile_loop"))
    tasks.append(asyncio.create_task(heartbeat_loop(), name="heartbeat_loop"))
    if settings.fb_catalog_id and settings.fb_catalog_access_token:
        tasks.append(asyncio.create_task(facebook_catalog_loop(), name="facebook_catalog_loop"))

    if logger:
        logger.info(f"Worker started with {len(tasks)} consumer task(s)")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # Every one of these tasks is a `while True` loop that is only supposed
    # to exit via cancellation during our own shutdown below. If one instead
    # exits on its own — an unhandled exception escaping before it reaches
    # its own internal try/except (e.g. StreamConsumer._ensure_group failing
    # before the protected read loop even starts) — that task silently stops
    # doing its job while this process keeps running and *looks* healthy.
    # That's worse than crashing: a zombie worker means payments/alerts/
    # reconciliation quietly stop with nothing surfacing it. So: watch every
    # task, and the moment any of them finishes unexpectedly, log it loudly
    # and exit non-zero so `restart: unless-stopped` actually recovers us.
    stop_task = asyncio.create_task(stop_event.wait(), name="stop_event_wait")
    watch_set: set[asyncio.Task] = set(tasks) | {stop_task}
    crashed = False

    while watch_set:
        done, watch_set = await asyncio.wait(watch_set, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            break
        for finished in done:
            exc = finished.exception() if not finished.cancelled() else None
            name = finished.get_name()
            message = (
                f"Worker task '{name}' exited unexpectedly: {exc}"
                if exc
                else f"Worker task '{name}' returned without being cancelled — it should run forever"
            )
            if logger:
                logger.error(message)
            try:
                await SystemLogService().log(
                    component="worker",
                    level="error",
                    message=message,
                    context={"task": name, "error": str(exc) if exc else None},
                )
            except Exception:
                pass
        crashed = True
        break

    if logger:
        logger.info("Worker shutting down..." if not crashed else "Worker shutting down after task crash...")
    stop_task.cancel()
    for task in tasks:
        task.cancel()
    await asyncio.gather(stop_task, *tasks, return_exceptions=True)

    await db.close()
    await redis_client.close()
    if logger:
        logger.info("Worker shutdown complete")

    if crashed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
