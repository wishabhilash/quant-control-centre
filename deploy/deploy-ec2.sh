#!/usr/bin/env bash
# Install a platform release on the EC2 instance.
#
# Run by the GitHub Actions deploy job (over SSH), as root. The script is
# idempotent and self-bootstrapping: it provisions what is missing (service
# user, directories, venv, the admin-token env file), installs the release,
# WRITES/ENABLES the systemd unit, restarts the service, health-checks it and
# reconciles the cron lines.
#
# Everything it touches lives under $APP_DIR/platform — the strategy tree
# ($STRATEGIES_DIR) and the runtime state are NEVER modified.
#
# Usage: deploy-ec2.sh <backend.tar.gz> <frontend-dist.tar.gz>
# Env:   APP_DIR=/opt/quant-control-centre  ENV_FILE=/etc/quant/backend.env
#        RELEASE_SHA=<sha>   DRY_RUN=1 (print actions instead of executing)
# APP_DIR must match PLATFORM_ROOT in the service env — the cron lines embed
# {PLATFORM_ROOT}/platform/backend/scripts/runner.py.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quant-control-centre}"
ENV_DIR="${ENV_DIR:-/etc/quant}"
ENV_FILE="${ENV_FILE:-$ENV_DIR/backend.env}"
SERVICE_NAME="${SERVICE_NAME:-quant-backend}"
SERVICE_USER="${SERVICE_USER:-quant}"
SERVICE_GROUP="${SERVICE_GROUP:-quant}"
UNIT_PATH="/etc/systemd/system/$SERVICE_NAME.service"
UNIT_ROOT_DEFAULT="${UNIT_ROOT_DEFAULT:-/opt/quant-control-centre}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
RELEASE_SHA="${RELEASE_SHA:-unknown}"
DRY_RUN="${DRY_RUN:-0}"
BACKEND_TGZ="${1:?usage: deploy-ec2.sh <backend.tar.gz> <frontend-dist.tar.gz>}"
FRONTEND_TGZ="${2:?usage: deploy-ec2.sh <backend.tar.gz> <frontend-dist.tar.gz>}"

BACKEND_DIR="$APP_DIR/platform/backend"
FRONTEND_DIR="$APP_DIR/platform/frontend"
RUNTIME_DIR="$APP_DIR/runtime"
VENV_DIR="$BACKEND_DIR/venv"
BACKUP_DIR="$APP_DIR/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '[deploy %s] %s\n' "$STAMP" "$*"; }
die() { printf '[deploy %s] ERROR: %s\n' "$STAMP" "$*" >&2; exit 1; }

# The deploy restarts systemd units, creates users and installs packages — it
# must run as root. CI usually SSHes in as an unprivileged user, so re-execute
# ourselves with sudo (non-interactive) when possible.
if [ "$(id -u)" != "0" ]; then
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    log "re-executing as root via sudo"
    exec sudo -n env \
      APP_DIR="$APP_DIR" ENV_DIR="$ENV_DIR" ENV_FILE="$ENV_FILE" \
      SERVICE_NAME="$SERVICE_NAME" SERVICE_USER="$SERVICE_USER" \
      SERVICE_GROUP="$SERVICE_GROUP" UNIT_ROOT_DEFAULT="$UNIT_ROOT_DEFAULT" \
      HEALTH_URL="$HEALTH_URL" RELEASE_SHA="$RELEASE_SHA" DRY_RUN="$DRY_RUN" \
      PYENV_ROOT="${PYENV_ROOT:-/opt/pyenv}" PYTHON_VERSION="${PYTHON_VERSION:-3.14}" \
      bash "$0" "$@"
  fi
  die "must run as root (or with passwordless sudo): the deploy restarts systemd units, creates the service user and installs packages"
fi

# Side-effecting commands go through run(): with DRY_RUN=1 they are printed
# instead of executed (review the flow without touching a host).
run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

# Shared provisioning library (toolchain + pyenv + Python).
# shellcheck source=deploy/lib/ensure-python.sh
[ -f "$SCRIPT_DIR/lib/ensure-python.sh" ] && . "$SCRIPT_DIR/lib/ensure-python.sh"

[ -f "$BACKEND_TGZ" ] || die "missing $BACKEND_TGZ"
[ -f "$FRONTEND_TGZ" ] || die "missing $FRONTEND_TGZ"

# --- 1. bootstrap the host (idempotent) ------------------------------------
log "ensuring the service user '$SERVICE_USER'"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  run useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  log "created the service user"
fi

log "ensuring the directory layout under $APP_DIR"
run install -d -m 755 "$APP_DIR" "$APP_DIR/platform" "$BACKEND_DIR" "$FRONTEND_DIR" \
  "$FRONTEND_DIR/dist" "$RUNTIME_DIR" "$RUNTIME_DIR/locks" "$RUNTIME_DIR/state" "$BACKUP_DIR"
# The service writes locks/state (and, via STRATEGIES_DIR, the strategy logs).
# Only these paths are handed over — the venv stays root-owned and read-only
# for the service (it only executes it), and a recursive chown over a venv
# would walk thousands of files for nothing.
run chown -R "$SERVICE_USER:$SERVICE_GROUP" "$RUNTIME_DIR" || log "WARNING: could not chown $RUNTIME_DIR"

log "ensuring the platform venv at $VENV_DIR"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  # Provision the toolchain + pyenv + Python when they are missing (a fresh
  # instance gets everything it needs from the deploy itself); then build the
  # venv with that interpreter.
  if [ "$DRY_RUN" = "1" ]; then
    log "[dry-run] would ensure pyenv + Python $PYTHON_VERSION"
    log "[dry-run] would create the venv at $VENV_DIR"
  else
    ensure_python
    PYTHON_RESOLVED="$(python_interpreter)" \
      || die "no python3 interpreter found (ensure_python should have installed one)"
    log "creating the venv with $PYTHON_RESOLVED"
    "$PYTHON_RESOLVED" -m venv "$VENV_DIR"
    # Drive pip through the venv's interpreter (see below) so the packages
    # cannot land in the base environment or in ~/.local.
    PIP_USER=0 PYTHONNOUSERSITE=1 "$VENV_DIR/bin/python" -m pip install \
      --disable-pip-version-check -q --upgrade pip setuptools wheel
  fi
else
  log "platform venv already present at $VENV_DIR"
fi

# The platform installs its schedules into the SYSTEM crontab — without a cron
# implementation the schedules would silently never fire.
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] would ensure the cron daemon (cronie) is installed and running"
else
  ensure_cron || log "WARNING: cron setup failed — installed schedules will not fire"
fi

if [ ! -f "$ENV_FILE" ]; then
  log "creating $ENV_FILE with a freshly generated admin token"
  run install -d -m 750 -o root -g "$SERVICE_GROUP" "$ENV_DIR"
  TOKEN="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  if [ "$DRY_RUN" = "1" ]; then
    log "[dry-run] would write DEV_API_TOKEN to $ENV_FILE (640 root:$SERVICE_GROUP)"
  else
    # The umask is scoped to THIS subshell only. A umask that leaks into the
    # later pip install would create the site-packages unreadable for the
    # service account (mode 700) — the platform would then fail to import
    # uvicorn and crash-loop with "No module named 'uvicorn.main'".
    (
      umask 077
      {
        echo "# Quant Strategy Control Centre — production environment (generated by the deploy)."
        echo "# 640 root:$SERVICE_GROUP — keep it out of git. The admin token below is what the"
        echo "# dashboard sends as 'Bearer <token>' (there is no login screen yet)."
        echo "DEV_API_TOKEN=$TOKEN"
      } > "$ENV_FILE"
    )
    run chown root:"$SERVICE_GROUP" "$ENV_FILE"
    run chmod 640 "$ENV_FILE"
    log "generated a new admin token — read it with: sudo cat $ENV_FILE"
  fi
fi

# Auth sanity check: with AUTH_ENABLED=true the dashboard needs a token, and
# the env file is mode 750 root:quant — a non-root reader can easily mistake
# "permission denied" for "the file does not exist".
if [ -f "$ENV_FILE" ]; then
  if grep -q '^DEV_API_TOKEN=' "$ENV_FILE"; then
    log "admin token configured in $ENV_FILE"
  else
    log "WARNING: $ENV_FILE has no DEV_API_TOKEN — the dashboard will answer 401"
  fi
else
  log "WARNING: $ENV_FILE not found — with AUTH_ENABLED=true every admin request fails"
fi

# --- 2. stage the release ---------------------------------------------------
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
log "staging release $RELEASE_SHA"
mkdir -p "$STAGE/backend" "$STAGE/frontend"
tar -xzf "$BACKEND_TGZ" -C "$STAGE/backend"
tar -xzf "$FRONTEND_TGZ" -C "$STAGE/frontend"

UNIT_TEMPLATE=""
for candidate in "$STAGE/backend/deploy/quant-backend.service" \
                 "$SCRIPT_DIR/systemd/quant-backend.service" \
                 "$BACKEND_DIR/deploy/quant-backend.service"; do
  [ -f "$candidate" ] && UNIT_TEMPLATE="$candidate" && break
done

# --- 3. back up the current release ------------------------------------------
mkdir -p "$BACKUP_DIR"
log "backing up the current release to $BACKUP_DIR/backend-$STAMP.tar.gz"
tar --exclude='venv' --exclude='.venv' --exclude='__pycache__' \
    -czf "$BACKUP_DIR/backend-$STAMP.tar.gz" -C "$BACKEND_DIR" .

# --- 4. install the backend code (venv and .env are preserved) --------------
log "installing backend code into $BACKEND_DIR"
run rsync -a --delete \
  --exclude='venv/' --exclude='.venv/' --exclude='.env' \
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='.pytest_cache/' \
  "$STAGE/backend/" "$BACKEND_DIR/"

log "installing backend dependencies into the platform venv"
# Drive pip THROUGH the venv's interpreter. `venv/bin/pip` carries a shebang
# that can point at another interpreter (then the packages land in the base
# environment, not the venv), and PIP_USER=1 in the environment would make pip
# install into ~/.local — invisible to the service account.
export PIP_USER=0
export PYTHONNOUSERSITE=1
run "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -q --no-cache-dir \
  --upgrade -r "$BACKEND_DIR/requirements.txt"

# The service account must be able to READ the venv (it only executes it).
# A restrictive umask during the install would leave site-packages at mode 700
# and the platform would fail to import its dependencies.
run chmod -R a+rX "$VENV_DIR"

# Verify the dependency set BEFORE restarting the service — and verify it AS
# THE SERVICE USER, because that is the account the platform actually runs as.
# An unreadable or half-installed package (e.g. an empty uvicorn/ directory)
# otherwise only shows up as a crash-loop "No module named 'uvicorn.main'".
if [ "$DRY_RUN" != "1" ]; then
  IMPORT_CHECK='import uvicorn.main, fastapi, boto3, croniter, pydantic_settings'
  VENV_CHECK='import sys; sys.exit(0 if sys.prefix == sys.argv[1] else 1)'
  if ! "$VENV_DIR/bin/python" -c "$VENV_CHECK" "$VENV_DIR" 2>/dev/null; then
    die "the interpreter at $VENV_DIR/bin/python is not a venv rooted there — remove $VENV_DIR and re-run"
  fi
  if ! runuser -u "$SERVICE_USER" -- "$VENV_DIR/bin/python" -c "$IMPORT_CHECK" 2>/dev/null; then
    # Rebuild the venv from scratch: repairing a half-installed package set in
    # place is unreliable (this is what broke the service).
    log "dependency verification failed as '$SERVICE_USER' — rebuilding the platform venv"
    rm -rf "$VENV_DIR"
    PYTHON_RESOLVED="$(python_interpreter)" \
      || die "no python3 interpreter found to rebuild the venv with"
    "$PYTHON_RESOLVED" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -q --no-cache-dir \
      --upgrade pip setuptools wheel
    "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --no-cache-dir -q \
      -r "$BACKEND_DIR/requirements.txt"
    chmod -R a+rX "$VENV_DIR"
    runuser -u "$SERVICE_USER" -- "$VENV_DIR/bin/python" -c "$IMPORT_CHECK" \
      || die "the rebuilt venv is still not importable as '$SERVICE_USER' — check the pip output above"
  fi
  "$VENV_DIR/bin/python" -m pip check >/dev/null 2>&1 || log "WARNING: pip check reported dependency issues"
  log "dependency verification passed (as '$SERVICE_USER')"
fi

# --- 5. install the frontend bundle -----------------------------------------
log "installing the frontend bundle into $FRONTEND_DIR/dist"
run rsync -a --delete "$STAGE/frontend/" "$FRONTEND_DIR/dist/"

# --- 6. write / update the systemd unit ------------------------------------
# The template's install root is rewritten to $APP_DIR, so a custom path works
# without editing the unit by hand. The unit is always written (created on the
# first deploy, refreshed afterwards) and enabled for boot.
[ -n "$UNIT_TEMPLATE" ] || die "no systemd unit template found in the release artifact"
RENDERED="$(mktemp)"
sed -e "s#${UNIT_ROOT_DEFAULT}#${APP_DIR}#g" -e "s#^EnvironmentFile=.*#EnvironmentFile=-${ENV_FILE}#" \
  "$UNIT_TEMPLATE" > "$RENDERED"
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] would write the systemd unit to $UNIT_PATH"
elif cmp -s "$RENDERED" "$UNIT_PATH"; then
  log "systemd unit unchanged"
else
  log "writing the systemd unit to $UNIT_PATH"
  run install -m 644 "$RENDERED" "$UNIT_PATH"
  run systemctl daemon-reload
fi
run systemctl enable "$SERVICE_NAME"

# The strategies volume is referenced by the unit — create it if missing and
# hand it to the service user (the platform marks it platform-owned on first
# use; it must start out empty or already marked).
STRATEGIES_DIR_EFFECTIVE="$(sed -n 's/^Environment=STRATEGIES_DIR=//p' "$RENDERED" | tail -1)"
if [ -n "$STRATEGIES_DIR_EFFECTIVE" ] && [ ! -d "$STRATEGIES_DIR_EFFECTIVE" ]; then
  log "creating the strategies directory $STRATEGIES_DIR_EFFECTIVE"
  run install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STRATEGIES_DIR_EFFECTIVE"
fi

# The service's environment (from the unit) is what the cron reconcile must
# use — export it now, from the rendered unit, for the reconcile step below.
while IFS= read -r line; do
  key="${line#Environment=}"
  case "$key" in
    *=*) export "${key%%=*}=${key#*=}";;
  esac
done < <(grep '^Environment=' "$RENDERED")
rm -f "$RENDERED"

# --- 7. record the release ----------------------------------------------------
echo "$RELEASE_SHA" > "$APP_DIR/platform/VERSION"

# --- 8. restart and health-check ----------------------------------------------
log "restarting $SERVICE_NAME"
run systemctl restart "$SERVICE_NAME"
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] would poll $HEALTH_URL"
else
  for i in $(seq 1 30); do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      log "health check passed ($HEALTH_URL)"
      break
    fi
    [ "$i" = "30" ] && {
      # Stop the restart storm before failing: a crash-looping unit would keep
      # restarting (and filling the journal) long after this deploy failed.
      log "health check failed — stopping $SERVICE_NAME to avoid a restart loop"
      systemctl stop "$SERVICE_NAME" || true
      die "service did not become healthy after restart — inspect with: journalctl -u $SERVICE_NAME -n 50 --no-pager"
    }
    sleep 1
  done
fi

# --- 9. refresh the cron lines (runner path / template may have changed) -----
# Run with the SERVICE's own environment (the unit's Environment= lines plus
# the env file) and as the service user — otherwise the reconcile would use
# dev defaults (wrong bucket, dry-run on).
log "reconciling the crontab"
set -a
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
set +a
( cd "$BACKEND_DIR" && \
  run runuser -u "$SERVICE_USER" -- "$VENV_DIR/bin/python" - <<'PY'
from app.core.config import get_settings
from app.services.scheduler_service import SchedulerService
result = SchedulerService(get_settings()).reconcile()
print("crontab reconciled:", result)
PY
) || log "WARNING: cron reconcile failed — check the service env"

log "deployed $RELEASE_SHA successfully (backup: $BACKUP_DIR/backend-$STAMP.tar.gz)"
