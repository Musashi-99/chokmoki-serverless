"""Manual E2E smoke test for the multi-admin ABAC/passwordless feature —
exercises the real HTTP server via `requests` (session cookies + CSRF,
exactly like a real browser), against real Mongo/Redis. Not part of the
pytest suite (see pytest.ini's python_files = test_*.py) — this is a
deliberate, deployment-shaped smoke test to run by hand against a
local/staging stack before/after touching admin auth.

Usage (inside a running backend container, or locally with MONGODB_URI/
REDIS_URL/ADMIN_EMAIL/ADMIN_PASSWORD/ADMIN_SECRET_ENCRYPTION_KEY pointed at
a real dev stack and `python scripts/migrate_root_admin.py --apply` already
run):
    python scripts/smoke_test_multi_admin.py

Exits non-zero on the first failed check.
"""
import sys

import pyotp
import requests

BASE = "http://localhost:8000/api/admin"
TIMEOUT = 10  # requests has no default timeout — a hung server must fail fast, not hang the test forever.


def csrf_header(session):
    token = session.cookies.get("chokmoki_csrf")
    return {"X-CSRF-Token": token} if token else {}


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        sys.exit(1)


def main():
    root = requests.Session()

    # 1. Root logs in with password (break-glass path, unchanged)
    r = root.post(f"{BASE}/login", json={"email": "root@chokmoki.com", "password": "RootPassw0rd!2026Xy"}, timeout=TIMEOUT)
    check("root password login succeeds", r.status_code == 200)
    body = r.json()
    check("root token carries is_root=true", body["is_root"] is True)
    check("root token carries wildcard scope", body["scopes"] == ["*"])

    # 2. Root invites a scoped, region-tagged admin
    r = root.post(
        f"{BASE}/admins",
        json={
            "email": "regional@chokmoki.com",
            "name": "Regional Admin",
            "role": "regional_admin",
            "scopes": ["orders:read", "inbox:read"],
            "region": "IN-MH",
        },
        headers=csrf_header(root),
        timeout=TIMEOUT,
    )
    check(f"root can invite a new admin (status={r.status_code})", r.status_code == 200)
    invited = r.json()
    admin_id = invited["id"]
    check("invited admin starts in 'invited' status", invited["status"] == "invited")

    # 3. Unauthenticated invite attempt is rejected outright
    anon = requests.Session()
    r = anon.post(
        f"{BASE}/admins",
        json={"email": "x@x.com", "name": "x", "role": "admin", "scopes": [], "region": None},
        timeout=TIMEOUT,
    )
    check(f"unauthenticated invite is rejected (status={r.status_code})", r.status_code == 401)

    # 4. Issue a fresh enrollment token directly via the service (in-process
    #    — stands in for "the email that would have been sent" when SMTP
    #    isn't configured in this environment) and complete enrollment.
    import asyncio

    from src.services.admin_user_service import AdminUserService

    async def issue_token():
        svc = AdminUserService()
        doc = await svc.get_by_email("regional@chokmoki.com")
        return await svc._issue_enrollment_token(doc["email"])

    token = asyncio.run(issue_token())

    r = requests.get(f"{BASE}/enroll/{token}", timeout=TIMEOUT)
    check(f"enrollment info fetch succeeds (status={r.status_code})", r.status_code == 200)
    provisioning_uri = r.json()["provisioning_uri"]
    secret = dict(p.split("=") for p in provisioning_uri.split("?", 1)[1].split("&"))["secret"]
    code = pyotp.TOTP(secret).now()

    r = requests.post(f"{BASE}/enroll/{token}/confirm", json={"code": code}, timeout=TIMEOUT)
    check(f"enrollment confirm succeeds (status={r.status_code}, body={r.text[:200]})", r.status_code == 200)

    # 5. Passwordless login as the newly enrolled admin
    regional = requests.Session()
    code2 = pyotp.TOTP(secret).now()
    r = regional.post(f"{BASE}/login", json={"email": "regional@chokmoki.com", "totp_code": code2}, timeout=TIMEOUT)
    check(f"passwordless login succeeds (status={r.status_code})", r.status_code == 200)
    rbody = r.json()
    check("regional admin scopes match invited scopes", set(rbody["scopes"]) == {"orders:read", "inbox:read"})
    check("regional admin is_root is false", rbody["is_root"] is False)
    check("regional admin region carried through", rbody["region"] == "IN-MH")

    # 6. ABAC in action against REAL routes: regional admin (orders:read +
    #    inbox:read only) must be denied products:read but allowed
    #    orders:read/inbox:read.
    r = regional.get(f"{BASE}/products", timeout=TIMEOUT)
    check(f"regional admin denied products:read (status={r.status_code})", r.status_code == 403)

    r = regional.get(f"{BASE}/orders", timeout=TIMEOUT)
    check(f"regional admin NOT denied by ABAC on orders:read (status={r.status_code})", r.status_code != 403)

    r = regional.get(f"{BASE}/inbox", timeout=TIMEOUT)
    check(f"regional admin NOT denied by ABAC on inbox:read (status={r.status_code})", r.status_code != 403)

    # 7. Regional admin cannot manage other admins
    r = regional.get(f"{BASE}/admins", timeout=TIMEOUT)
    check(f"regional admin denied admins:read (status={r.status_code})", r.status_code == 403)

    # 8. login/mode precheck distinguishes root vs invited vs unknown
    r = requests.get(f"{BASE}/login/mode", params={"email": "root@chokmoki.com"}, timeout=TIMEOUT)
    check("login/mode: root -> password", r.json()["mode"] == "password")
    r = requests.get(f"{BASE}/login/mode", params={"email": "regional@chokmoki.com"}, timeout=TIMEOUT)
    check("login/mode: regional -> passwordless", r.json()["mode"] == "passwordless")
    r = requests.get(f"{BASE}/login/mode", params={"email": "nobody@nowhere.com"}, timeout=TIMEOUT)
    check("login/mode: unknown email -> unknown", r.json()["mode"] == "unknown")

    # 9. Root deactivates the regional admin; their existing session dies
    #    immediately and they cannot log back in.
    r = root.post(f"{BASE}/admins/{admin_id}/deactivate", headers=csrf_header(root), timeout=TIMEOUT)
    check(f"root can deactivate regional admin (status={r.status_code})", r.status_code == 200)

    r2 = regional.get(f"{BASE}/orders", timeout=TIMEOUT)
    check(f"deactivated admin's existing session is now rejected (status={r2.status_code})", r2.status_code == 401)

    code3 = pyotp.TOTP(secret).now()
    r3 = requests.post(f"{BASE}/login", json={"email": "regional@chokmoki.com", "totp_code": code3}, timeout=TIMEOUT)
    check(f"deactivated admin cannot log back in (status={r3.status_code})", r3.status_code == 401)

    # 10. Root itself cannot be deactivated via the API (hard invariant)
    r = root.get(f"{BASE}/admins", timeout=TIMEOUT)
    root_row = next(a for a in r.json() if a["is_root"])
    r = root.post(f"{BASE}/admins/{root_row['id']}/deactivate", headers=csrf_header(root), timeout=TIMEOUT)
    check(f"root account cannot be deactivated via API (status={r.status_code})", r.status_code == 400)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
