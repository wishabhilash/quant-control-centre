#!/usr/bin/env bash
# Install a platform release on the EC2 instance.
#
# Run by the GitHub Actions deploy job (over SSH), as root (it restarts a
# systemd unit). Everything it touches lives under $APP_DIR/platform — the
# strategy tree ($STRATEGIES_DIR, e.g. /var/lib/quant/strategies) and the
# runtime state are NEVER modified.
#
# Usage: deploy-ec2.sh <backend.tar.gz> <frontend-dist.tar.gz>
# Env:   APP_DIR=/opt/quant-control-centre  ENV_FILE=/etc/quant/backend.env
#        RELEASE_SHA=<sha>
# APP_DIR must match PLATFORM_ROOT in the service env file — the cron lines
# embed {PLATFORM_ROOT}/platform/backend/scripts/runner.py.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quant-control-centre}"
ENV_FILE="${ENV_FILE:-/etc/quant/backend.env}"
RELEASE_SHA="${RELEASE_SHA:-unknown}"
BACKEND_TGZ="${1:?usage: deploy-ec2.sh <backend.tar.gz> <frontend-dist.tar.gz>}"
FRONTEND_TGZ="${2:?usage: deploy-ec2.sh <backend.tar.gz> <frontend-dist.tar.gz>}"

BACKEND_DIR="$APP_DIR/platform/backend"
FRONTEND_DIR="$APP_DIR/platform/frontend"
VENV_DIR="$BACKEND_DIR/venv"
BACKUP_DIR="$APP_DIR/backups"
UNIT_ROOT_DEFAULT="${UNIT_ROOT_DEFAULT:-/opt/quant-control-centre}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

log() { printf '[deploy %s] %s\n' "$STAMP" "$*"; }
die() { printf '[deploy %s] ERROR: %s\n' "$STAMP" "$*" >&2; exit 1; }

[ -f "$BACKEND_TGZ" ] || die "missing $BACKEND_TGZ"
[ -f "$FRONTEND_TGZ" ] || die "missing $FRONTEND_TGZ"
[ -d "$BACKEND_DIR" ] || die "platform not installed at $BACKEND_DIR (bootstrap it first)"
[ -x "$VENV_DIR/bin/python" ] || die "platform venv missing at $VENV_DIR"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

log "staging release $RELEASE_SHA"
mkdir -p "$STAGE/backend" "$STAGE/frontend"
tar -xzf "$BACKEND_TGZ" -C "$STAGE/backend"
tar -xzf "$FRONTEND_TGZ" -C "$STAGE/frontend"

# The systemd unit travels with the artifact; fall back to the installed copy.
UNIT_TEMPLATE=""
for candidate in "$STAGE/backend/deploy/quant-backend.service" \
                 "$BACKEND_DIR/deploy/quant-backend.service"; do
  [ -f "$candidate" ] && UNIT_TEMPLATE="$candidate" && break
done

# --- 1. backup the current release (rollback material) ----------------------
mkdir -p "$BACKUP_DIR"
log "backing up the current release to $BACKUP_DIR/backend-$STAMP.tar.gz"
tar --exclude='venv' --exclude='.venv' --exclude='__pycache__' \
    -czf "$BACKUP_DIR/backend-$STAMP.tar.gz" -C "$BACKEND_DIR" .

# --- 2. install the backend code (venv and .env are preserved) --------------
log "installing backend code into $BACKEND_DIR"
rsync -a --delete \
  --exclude='venv/' --exclude='.venv/' --exclude='.env' \
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='.pytest_cache/' \
  "$STAGE/backend/" "$BACKEND_DIR/"

log "installing backend dependencies into the platform venv"
"$VENV_DIR/bin/pip" install --disable-pip-version-check -q -r "$BACKEND_DIR/requirements.txt"

# --- 3. install the frontend bundle ---------------------------------------
log "installing the frontend bundle into $FRONTEND_DIR/dist"
mkdir -p "$FRONTEND_DIR/dist"
rsync -a --delete "$STAGE/frontend/" "$FRONTEND_DIR/dist/"

# --- 4. systemd unit (only when it changed) --------------------------------
# The template's paths are rewritten to $APP_DIR so a custom install root
# (e.g. /opt/quant-control-centre) works without editing the unit by hand.
if [ -n "$UNIT_TEMPLATE" ]; then
  RENDERED="$(mktemp)"
  sed "s#${UNIT_ROOT_DEFAULT}#${APP_DIR}#g" "$UNIT_TEMPLATE" > "$RENDERED"
  if ! cmp -s "$RENDERED" /etc/systemd/system/quant-backend.service; then
    log "updating the systemd unit"
    install -m 644 "$RENDERED" /etc/systemd/system/quant-backend.service
    systemctl daemon-reload
  fi
  rm -f "$RENDERED"
fi

# --- 5. record the release --------------------------------------------------
echo "$RELEASE_SHA" > "$APP_DIR/platform/VERSION"

# --- 6. restart and health-check -------------------------------------------
log "restarting quant-backend"
systemctl restart quant-backend
for i in $(seq 1 30); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    log "health check passed"
    break
  fi
  [ "$i" = "30" ] && die "service did not become healthy after restart"
  sleep 1
done

# --- 7. refresh the cron lines (runner path / template may have changed) ---
# Run with the service's own environment and as the service user.
if [ -f "$ENV_FILE" ]; then
  log "reconciling the crontab"
  set -a; . "$ENV_FILE"; set +a
  ( cd "$BACKEND_DIR" && \
    runuser -u quant -- "$VENV_DIR/bin/python" - <<'PY'
from app.core.config import get_settings
from app.services.scheduler_service import SchedulerService
result = SchedulerService(get_settings()).reconcile()
print("crontab reconciled:", result)
PY
  ) || log "WARNING: cron reconcile failed — check the service env"
else
  log "WARNING: $ENV_FILE not found — skipping the cron reconcile"
fi

log "deployed $RELEASE_SHA successfully (backup: $BACKUP_DIR/backend-$STAMP.tar.gz)"
