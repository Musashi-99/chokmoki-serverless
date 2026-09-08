#!/usr/bin/env bash
# Deploy chokmoki-serverless + chokmoki-ui from /home/deploy on the server.
#
# Codifies the exact manual sequence used to ship the multi-admin ABAC
# feature: pull both repos, make sure production secrets required by the
# new code exist (ADMIN_SECRET_ENCRYPTION_KEY), rebuild + restart the
# backend/worker/frontend containers, run the (idempotent) admin_users
# migrations, then verify health both from inside the containers and over
# the real public domains.
#
# Usage (on the server, as the `deploy` user):
#   cd /home/deploy/chokmoki-serverless
#   ./deploy/deploy.sh
#
# Flags:
#   --skip-frontend   Only deploy the backend (chokmoki-serverless).
#   --force           Proceed even if either repo has uncommitted local
#                      changes (normally this aborts — see below).
#
# Safe to re-run: every step here is idempotent (git pull on a clean tree,
# `docker compose build` only rebuilds what changed, the migration scripts
# no-op if already applied).
set -euo pipefail

DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND_DIR="$DEPLOY_ROOT/chokmoki-serverless"
FRONTEND_DIR="$DEPLOY_ROOT/chokmoki-ui"

SKIP_FRONTEND=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --skip-frontend) SKIP_FRONTEND=1 ;;
    --force) FORCE=1 ;;
    *) echo "Unknown flag: $arg" >&2; exit 1 ;;
  esac
done

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
fail() { printf '\033[1;31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

[ -d "$BACKEND_DIR/.git" ] || fail "Expected $BACKEND_DIR to be a git checkout — wrong DEPLOY_ROOT?"
if [ "$SKIP_FRONTEND" -eq 0 ]; then
  [ -d "$FRONTEND_DIR/.git" ] || fail "Expected $FRONTEND_DIR to be a git checkout (use --skip-frontend to deploy backend only)"
fi

check_clean() {
  local dir="$1"
  if [ "$FORCE" -eq 1 ]; then return 0; fi
  if [ -n "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; then
    fail "$dir has uncommitted local changes — commit/stash them or re-run with --force"
  fi
}

# ---- 1. Pull ----------------------------------------------------------
log "Pulling $BACKEND_DIR"
check_clean "$BACKEND_DIR"
git -C "$BACKEND_DIR" pull --ff-only origin main

if [ "$SKIP_FRONTEND" -eq 0 ]; then
  log "Pulling $FRONTEND_DIR"
  check_clean "$FRONTEND_DIR"
  git -C "$FRONTEND_DIR" pull --ff-only origin main
fi

# ---- 2. Make sure production secrets required by current code exist ---
# Settings() fails fast at import time in ENVIRONMENT=production if any
# required secret is missing (src/security/secrets_validation.py) — that
# would crash-loop the backend/worker containers on restart, so this must
# happen BEFORE rebuilding/restarting them, not after.
log "Checking backend .env for required secrets"
ENV_FILE="$BACKEND_DIR/.env"
[ -f "$ENV_FILE" ] || fail "$ENV_FILE does not exist"

if ! grep -q '^ADMIN_SECRET_ENCRYPTION_KEY=' "$ENV_FILE"; then
  echo "ADMIN_SECRET_ENCRYPTION_KEY missing — generating one (used to encrypt per-admin TOTP secrets at rest)"
  KEY="$(docker run --rm python:3.12-slim-bookworm python -c \
    "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())")"
  cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
  printf 'ADMIN_SECRET_ENCRYPTION_KEY=%s\n' "$KEY" >> "$ENV_FILE"
  echo "Appended a new key (previous .env backed up alongside it)."
else
  echo "ADMIN_SECRET_ENCRYPTION_KEY already set."
fi

# ---- 3. Build + restart backend/worker ---------------------------------
log "Building backend + worker images"
(cd "$BACKEND_DIR" && docker compose build backend worker)

log "Restarting backend + worker"
(cd "$BACKEND_DIR" && docker compose up -d backend worker)

log "Waiting for backend to report healthy"
for i in $(seq 1 30); do
  status="$(docker inspect -f '{{.State.Health.Status}}' chokmoki-backend 2>/dev/null || echo "starting")"
  [ "$status" = "healthy" ] && break
  sleep 2
  if [ "$i" -eq 30 ]; then fail "backend did not become healthy within 60s — check: docker logs chokmoki-backend"; fi
done
echo "backend is healthy."

# ---- 4. Run admin_users migrations (idempotent) ------------------------
log "Running admin_users migrations"
docker exec chokmoki-backend python scripts/migrate_root_admin.py --apply
docker exec chokmoki-backend python scripts/migrate_region_to_regions.py --apply

# ---- 5. Build + restart frontend ---------------------------------------
if [ "$SKIP_FRONTEND" -eq 0 ]; then
  log "Building frontend image"
  (cd "$FRONTEND_DIR" && docker compose build frontend)

  log "Restarting frontend"
  (cd "$FRONTEND_DIR" && docker compose up -d frontend)
fi

# ---- 6. Health checks ---------------------------------------------------
log "Container status"
docker ps --format 'table {{.Names}}\t{{.Status}}' \
  --filter name=chokmoki-backend --filter name=chokmoki-worker --filter name=chokmoki-ui-frontend

log "Public health checks"
curl -fsS -o /dev/null -w 'chokmoki.com: %{http_code}\n' https://chokmoki.com/ || echo "chokmoki.com check FAILED"
curl -fsS -o /dev/null -w 'api.chokmoki.com/health/ready: %{http_code}\n' https://api.chokmoki.com/health/ready || echo "api health check FAILED"

log "Deploy complete."
