#!/usr/bin/env bash
# Bootstrap an Amazon Linux EC2 instance for the Quant Strategy Control Centre.
#
# Optional: deploy/deploy-ec2.sh provisions the same Python toolchain on
# demand, so a fresh instance can be brought up by the CI deploy alone. Run
# this when you want the host prepared up front (or to install the pieces the
# deploy does not: the service user, the layout, crond).
#
# Run as root on the instance:
#   sudo bash deploy/bootstrap-ec2.sh
#
# Env:
#   PYTHON_VERSION=3.14        Python version to install via pyenv
#   APP_DIR=/opt/quant-control-centre     install root (must match PLATFORM_ROOT)
#   STRATEGIES_DIR=/var/lib/quant-control-centre/strategies   strategy volume
#   PYENV_ROOT=/opt/pyenv      shared pyenv installation (readable by all users)
#   SERVICE_USER=quant         service account
#   DRY_RUN=1                  print the plan instead of executing it
set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.14}"
STRATEGY_PYTHON_VERSION="${STRATEGY_PYTHON_VERSION:-3.12}"
APP_DIR="${APP_DIR:-/opt/quant-control-centre}"
STRATEGIES_DIR="${STRATEGIES_DIR:-/var/lib/quant-control-centre/strategies}"
PYENV_ROOT="${PYENV_ROOT:-/opt/pyenv}"
SERVICE_USER="${SERVICE_USER:-quant}"
SERVICE_GROUP="${SERVICE_GROUP:-quant}"
ENV_DIR="${ENV_DIR:-/etc/quant}"
DRY_RUN="${DRY_RUN:-0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }
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

if [ "$(id -u)" != "0" ] && [ "$DRY_RUN" != "1" ]; then
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    log "re-executing as root via sudo"
    exec sudo -n env \
      APP_DIR="$APP_DIR" ENV_DIR="$ENV_DIR" STRATEGIES_DIR="$STRATEGIES_DIR" \
      SERVICE_USER="$SERVICE_USER" SERVICE_GROUP="$SERVICE_GROUP" \
      PYENV_ROOT="$PYENV_ROOT" PYTHON_VERSION="$PYTHON_VERSION" \
      STRATEGY_PYTHON_VERSION="$STRATEGY_PYTHON_VERSION" DRY_RUN="$DRY_RUN" \
      bash "$0" "$@"
  fi
  die "run as root (sudo bash deploy/bootstrap-ec2.sh) — passwordless sudo is required for an unprivileged user"
fi

# --- 1. cron (the platform installs its own crontab entries) ----------------
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] would ensure the cron daemon (cronie) is installed and running"
else
  ensure_cron || log "WARNING: cron setup failed — installed schedules will not fire"
fi

# --- 2. toolchain + pyenv + Python ------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] would ensure the build toolchain, pyenv and Python $PYTHON_VERSION"
  log "[dry-run] would also install Python $STRATEGY_PYTHON_VERSION for the strategy venvs"
else
  ensure_python
fi

# --- 3. service user and directory layout ------------------------------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  log "creating the service user '$SERVICE_USER'"
  run useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

log "creating the directory layout"
run install -d -m 755 "$APP_DIR" "$APP_DIR/platform" "$APP_DIR/platform/backend" \
  "$APP_DIR/platform/frontend" "$APP_DIR/platform/frontend/dist" \
  "$APP_DIR/runtime" "$APP_DIR/runtime/locks" "$APP_DIR/runtime/state" \
  "$APP_DIR/backups" "$ENV_DIR"
run install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STRATEGIES_DIR"
run chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR/runtime" "$APP_DIR/platform" "$STRATEGIES_DIR"

# --- 4. the platform venv (built with the installed Python) ------------------
VENV_DIR="$APP_DIR/platform/backend/venv"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  log "creating the platform venv at $VENV_DIR (python $PYTHON_VERSION)"
  if [ "$DRY_RUN" = "1" ]; then
    run pyenv exec python -m venv "$VENV_DIR"
  else
    PYTHON_RESOLVED="$(python_interpreter)" \
      || die "no python3 interpreter found (ensure_python should have installed one)"
    "$PYTHON_RESOLVED" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --disable-pip-version-check -q --upgrade pip setuptools wheel
  fi
else
  log "platform venv already exists at $VENV_DIR"
fi

log "----------------------------------------------------------------"
log "bootstrap complete."
log "  Python      : $PYTHON_VERSION (pyenv root $PYENV_ROOT)"
log "  app dir     : $APP_DIR"
log "  strategies  : $STRATEGIES_DIR"
log "  service user: $SERVICE_USER"
log "Next: push to main (the CI deploy installs the code, writes the systemd"
log "      unit, installs the Python dependencies and restarts the service)."
log "NOTE: keep PYTHON_VERSION stable — every strategy venv references the"
log "      exact interpreter path under $PYENV_ROOT/versions/<version>."
