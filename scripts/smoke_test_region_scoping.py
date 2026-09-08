"""Manual smoke test: region-scoped admin order visibility. Seeds two
minimal order documents directly (IN and AU), invites a regional admin
scoped to IN, and verifies they only ever see the IN order through the real
HTTP API — list, stats, get-by-id, and mutation routes all enforce it.

Not part of the pytest suite. Run inside a container with a clean Mongo/
Redis and a root admin already migrated:
    python scripts/smoke_test_region_scoping.py
"""
import sys
import uuid

import pyotp
import requests

BASE = "http://localhost:8000/api/admin"
TIMEOUT = 10


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        sys.exit(1)


def csrf_header(session):
    token = session.cookies.get("chokmoki_csrf")
    return {"X-CSRF-Token": token} if token else {}


async def _seed_orders():
    from datetime import datetime

    from src.database.connection import db

    database = await db.get_database()
    orders = database["orders"]
    now = datetime.utcnow()

    def make(order_id, country):
        return {
            "order_id": order_id,
            "user_email": "customer@example.com",
            "shipping_address": {
                "email": "customer@example.com",
                "full_name": "Test Customer",
                "phone": "9999999999",
                "address_line1": "1 Test Street",
                "city": "Test City",
                "state": "Test State",
                "postal_code": "000000",
                "country": country if country != "default" else "US",
            },
            "items": [
                {
                    "product_id": "test-product",
                    "product_name": "Test Product",
                    "variant": {},
                    "quantity": 1,
                    "unit_price": 1000,
                    "total_price": 1000,
                }
            ],
            "subtotal": 1000,
            "discount": 0,
            "shipping": 0,
            "total_amount": 1000,
            "status": {"type": "accepted"},
            "payment_status": "completed",
            "shipment_status": "pending",
            "fulfillment_status": "pending",
            "created_at": now,
            "raw_order_log": {},
            "region_audit": {"pricing_country_used": country},
        }

    in_id = f"IN-{uuid.uuid4().hex[:8]}"
    au_id = f"AU-{uuid.uuid4().hex[:8]}"
    nz_id = f"NZ-{uuid.uuid4().hex[:8]}"
    await orders.insert_one(make(in_id, "IN"))
    await orders.insert_one(make(au_id, "AU"))
    await orders.insert_one(make(nz_id, "NZ"))
    return in_id, au_id, nz_id


def _run_async(coro):
    """Each Motor client is bound to the event loop it first connects on —
    plain back-to-back asyncio.run() calls in one process reuse the same
    MongoSingleton/RedisSingleton across a closed loop otherwise. Reset both
    between calls so this script (many small async steps interleaved with
    sync `requests` calls) can safely call asyncio.run() more than once."""
    import asyncio

    from src.database.connection import db as _mongo
    from src.database.redis_connection import redis_client as _redis

    _mongo._client = None
    _redis._client = None
    _redis._connection_pool = None
    return asyncio.run(coro)


def main():
    in_order_id, au_order_id, nz_order_id = _run_async(_seed_orders())
    print(f"seeded orders: IN={in_order_id} AU={au_order_id} NZ={nz_order_id}")

    root = requests.Session()
    r = root.post(f"{BASE}/login", json={"email": "root@chokmoki.com", "password": "RootPassw0rd!2026Xy"}, timeout=TIMEOUT)
    check("root login", r.status_code == 200)

    r = root.get(f"{BASE}/regions", timeout=TIMEOUT)
    check(f"regions endpoint (status={r.status_code})", r.status_code == 200)
    codes = {x["code"] for x in r.json()["data"]}
    check(f"regions include IN/AU/NZ/default (got {codes})", {"IN", "AU", "NZ", "default"} <= codes)

    r = root.post(
        f"{BASE}/admins",
        json={
            "email": "regional-in@chokmoki.com",
            "name": "Regional IN",
            "role": "regional_admin",
            "scopes": ["orders:read", "orders:write"],
            "regions": ["IN"],
        },
        headers=csrf_header(root),
        timeout=TIMEOUT,
    )
    check(f"invite regional IN admin (status={r.status_code})", r.status_code == 200)

    # Reject invite missing region for a regional_admin role.
    r = root.post(
        f"{BASE}/admins",
        json={
            "email": "regional-noregion@chokmoki.com",
            "name": "No Region",
            "role": "regional_admin",
            "scopes": ["orders:read"],
            "regions": [],
        },
        headers=csrf_header(root),
        timeout=TIMEOUT,
    )
    check(f"regional_admin without region is rejected (status={r.status_code})", r.status_code == 400)

    # Reject an unknown region code outright.
    r = root.post(
        f"{BASE}/admins",
        json={
            "email": "regional-bad@chokmoki.com",
            "name": "Bad Region",
            "role": "regional_admin",
            "scopes": ["orders:read"],
            "regions": ["ZZ"],
        },
        headers=csrf_header(root),
        timeout=TIMEOUT,
    )
    check(f"unknown region code is rejected (status={r.status_code})", r.status_code == 400)

    async def issue_token():
        from src.services.admin_user_service import AdminUserService

        svc = AdminUserService()
        doc = await svc.get_by_email("regional-in@chokmoki.com")
        return await svc._issue_enrollment_token(doc["email"])

    token = _run_async(issue_token())
    r = requests.get(f"{BASE}/enroll/{token}", timeout=TIMEOUT)
    provisioning_uri = r.json()["provisioning_uri"]
    secret = dict(p.split("=") for p in provisioning_uri.split("?", 1)[1].split("&"))["secret"]
    r = requests.post(f"{BASE}/enroll/{token}/confirm", json={"code": pyotp.TOTP(secret).now()}, timeout=TIMEOUT)
    check(f"regional admin enrollment confirm (status={r.status_code})", r.status_code == 200)

    regional = requests.Session()
    r = regional.post(f"{BASE}/login", json={"email": "regional-in@chokmoki.com", "totp_code": pyotp.TOTP(secret).now()}, timeout=TIMEOUT)
    check(f"regional admin passwordless login (status={r.status_code})", r.status_code == 200)
    check("regional admin regions == [IN]", r.json()["regions"] == ["IN"])

    # --- List: only the IN order shows up, and passing country=AU can't widen it ---
    r = regional.get(f"{BASE}/orders", timeout=TIMEOUT)
    check(f"regional admin list orders (status={r.status_code})", r.status_code == 200)
    ids = {o["order_id"] for o in r.json()["data"]}
    check(f"list shows only IN order (got {ids})", in_order_id in ids and au_order_id not in ids)

    r = regional.get(f"{BASE}/orders", params={"country": "AU"}, timeout=TIMEOUT)
    ids2 = {o["order_id"] for o in r.json()["data"]}
    check("client-supplied country=AU cannot widen visibility", au_order_id not in ids2)

    # --- Get by id: IN allowed, AU 404s (not 403 — no existence leak) ---
    r = regional.get(f"{BASE}/orders/{in_order_id}", timeout=TIMEOUT)
    check(f"regional admin can fetch own-region order (status={r.status_code})", r.status_code == 200)

    r = regional.get(f"{BASE}/orders/{au_order_id}", timeout=TIMEOUT)
    check(f"regional admin denied other-region order (status={r.status_code})", r.status_code == 404)

    # --- Stats: totalOrders reflects only the IN order ---
    r = regional.get(f"{BASE}/stats", timeout=TIMEOUT)
    check(f"regional admin stats (status={r.status_code})", r.status_code == 200)
    check(f"stats totalOrders == 1 for regional admin (got {r.json()['totalOrders']})", r.json()["totalOrders"] == 1)

    r = root.get(f"{BASE}/stats", timeout=TIMEOUT)
    check(f"root stats sees both orders (got {r.json()['totalOrders']})", r.json()["totalOrders"] >= 2)

    # --- Mutation on other-region order is also denied, not silently allowed ---
    r = regional.post(f"{BASE}/orders/{au_order_id}/notes", json={"text": "hi"}, headers=csrf_header(regional), timeout=TIMEOUT)
    check(f"regional admin cannot annotate other-region order (status={r.status_code})", r.status_code == 404)

    r = regional.post(f"{BASE}/orders/{in_order_id}/notes", json={"text": "hi"}, headers=csrf_header(regional), timeout=TIMEOUT)
    check(f"regional admin can annotate own-region order (status={r.status_code})", r.status_code == 200)

    # --- Multi-region admin (IN + AU): the actual feature being requested —
    # one admin covering more than one region. Sees both IN and AU orders,
    # still denied NZ.
    r = root.post(
        f"{BASE}/admins",
        json={
            "email": "regional-inau@chokmoki.com",
            "name": "Regional IN+AU",
            "role": "regional_admin",
            "scopes": ["orders:read"],
            "regions": ["IN", "AU"],
        },
        headers=csrf_header(root),
        timeout=TIMEOUT,
    )
    check(f"invite multi-region (IN+AU) admin (status={r.status_code})", r.status_code == 200)

    async def issue_token_multi():
        from src.services.admin_user_service import AdminUserService

        svc = AdminUserService()
        doc = await svc.get_by_email("regional-inau@chokmoki.com")
        return await svc._issue_enrollment_token(doc["email"])

    token2 = _run_async(issue_token_multi())
    r = requests.get(f"{BASE}/enroll/{token2}", timeout=TIMEOUT)
    provisioning_uri2 = r.json()["provisioning_uri"]
    secret2 = dict(p.split("=") for p in provisioning_uri2.split("?", 1)[1].split("&"))["secret"]
    r = requests.post(f"{BASE}/enroll/{token2}/confirm", json={"code": pyotp.TOTP(secret2).now()}, timeout=TIMEOUT)
    check(f"multi-region admin enrollment confirm (status={r.status_code})", r.status_code == 200)

    multi = requests.Session()
    r = multi.post(
        f"{BASE}/login", json={"email": "regional-inau@chokmoki.com", "totp_code": pyotp.TOTP(secret2).now()}, timeout=TIMEOUT
    )
    check(f"multi-region admin login (status={r.status_code})", r.status_code == 200)
    check("multi-region admin regions == [IN, AU]", set(r.json()["regions"]) == {"IN", "AU"})

    r = multi.get(f"{BASE}/orders", timeout=TIMEOUT)
    multi_ids = {o["order_id"] for o in r.json()["data"]}
    check(
        f"multi-region admin sees both IN and AU orders, not NZ (got {multi_ids})",
        in_order_id in multi_ids and au_order_id in multi_ids and nz_order_id not in multi_ids,
    )

    r = multi.get(f"{BASE}/orders/{nz_order_id}", timeout=TIMEOUT)
    check(f"multi-region admin denied the NZ order (status={r.status_code})", r.status_code == 404)

    r = multi.get(f"{BASE}/stats", timeout=TIMEOUT)
    check(f"multi-region admin stats cover both regions (got {r.json()['totalOrders']})", r.json()["totalOrders"] == 2)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
