#!/usr/bin/env bash
# Bootstrap an Amazon Linux EC2 instance for the Quant Strategy Control Centre.
#
# Installs the OS build toolchain, pyenv with the requested Python version
# (default 3.14), the `quant` service user, the directory layout, the platform
# venv and the pyenv PATH profile. The platform CODE and the systemd unit are
# installed afterwards by the CI deploy (deploy/deploy-ec2.sh), which also
# installs the Python dependencies and restarts the service.
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
APP_DIR="${APP_DIR:-/opt/quant-control-centre}"
STRATEGIES_DIR="${STRATEGIES_DIR:-/var/lib/quant-control-centre/strategies}"
PYENV_ROOT="${PYENV_ROOT:-/opt/pyenv}"
SERVICE_USER="${SERVICE_USER:-quant}"
SERVICE_GROUP="${SERVICE_GROUP:-quant}"
ENV_DIR="${ENV_DIR:-/etc/quant}"
DRY_RUN="${DRY_RUN:-0}"

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }
run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

if [ "$(id -u)" != "0" ] && [ "$DRY_RUN" != "1" ]; then
  die "run as root (sudo bash deploy/bootstrap-ec2.sh)"
fi

# --- 1. OS packages ----------------------------------------------------------
if command -v dnf >/dev/null 2>&1; then
  PKG_MGR=dnf
elif command -v yum >/dev/null 2>&1; then
  PKG_MGR=yum
elif [ "$DRY_RUN" = "1" ]; then
  PKG_MGR=""   # dry run on a non-Amazon-Linux host (e.g. a laptop) — plan only
  log "WARNING: no dnf/yum found (expected on Amazon Linux) — dry run continues"
else
  die "unsupported distribution: neither dnf nor yum found (Amazon Linux expected)"
fi
log "detected ${PKG_MGR:-<none>}"

# Build toolchain for compiling CPython + wheels that have no prebuilt binary.
PACKAGES=(gcc gcc-c++ make openssl-devel bzip2-devel libffi-devel zlib-devel
          readline-devel sqlite-devel xz-devel ncurses-devel gdbm-devel
          uuidlib-devel tk-devel git curl tar findutils patch which
          iproute procps-ng cronie cronie-anacron)

log "installing OS packages: ${PACKAGES[*]}"
if [ "$DRY_RUN" = "1" ]; then
  [ -n "$PKG_MGR" ] && run "$PKG_MGR" -y install "${PACKAGES[@]}"
else
  "$PKG_MGR" -y install "${PACKAGES[@]}" || {
    # cronie-anacron does not exist on every AL image — retry without it.
    log "retrying without cronie-anacron"
    "$PKG_MGR" -y install "${PACKAGES[@]/cronie-anacron/}"
  }
fi

# --- 2. pyenv + the Python version ------------------------------------------
if [ ! -d "$PYENV_ROOT/.git" ]; then
  log "installing pyenv into $PYENV_ROOT"
  run git clone --depth 1 https://github.com/pyenv/pyenv.git "$PYENV_ROOT"
else
  log "pyenv already present at $PYENV_ROOT"
fi

# pyenv build dependencies (readline, openssl, sqlite, xz, zlib) are provided by
# the packages installed above.
export PYENV_ROOT
export PATH="$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH"

if [ "$DRY_RUN" != "1" ]; then
  log "installing Python $PYTHON_VERSION (compiling from source — a few minutes)"
  "$PYENV_ROOT/bin/pyenv" install --skip-existing "$PYTHON_VERSION"
  "$PYENV_ROOT/bin/pyenv" global "$PYTHON_VERSION"
  "$PYENV_ROOT/bin/pyenv" rehash
  log "installed $(pyenv exec python --version 2>&1)"
  log "upgrading pip/setuptools/wheel in the $PYTHON_VERSION environment"
  pyenv exec python -m pip install --upgrade pip setuptools wheel
fi

# The profile so interactive shells and (via PATH) the service find the shims.
if [ "$DRY_RUN" != "1" ]; then
  cat > /etc/profile.d/pyenv.sh <<EOF
# Managed by deploy/bootstrap-ec2.sh — pyenv shims for all users.
export PYENV_ROOT="$PYENV_ROOT"
export PATH="\$PYENV_ROOT/shims:\$PYENV_ROOT/bin:\$PATH"
EOF
  chmod 644 /etc/profile.d/pyenv.sh
  # Everyone may read/execute the shared pyenv installation (the service user
  # creates strategy venvs with it).
  chmod -R a+rX "$PYENV_ROOT"
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
    pyenv exec python -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --disable-pip-version-check -q --upgrade pip setuptools wheel
  fi
else
  log "platform venv already exists at $VENV_DIR"
fi

# --- 5. cron (the platform installs its own crontab entries) ----------------
if command -v systemctl >/dev/null 2>&1; then
  log "enabling crond"
  run systemctl enable --now crond
fi

log "----------------------------------------------------------------"
log "bootstrap complete."
log "  Python      : $( [ "$DRY_RUN" = "1" ] && echo "$PYTHON_VERSION" || pyenv exec python --version 2>&1 )"
log "  pyenv root  : $PYENV_ROOT (shims on PATH via /etc/profile.d/pyenv.sh)"
log "  app dir     : $APP_DIR"
log "  strategies  : $STRATEGIES_DIR"
log "  service user: $SERVICE_USER"
log "NOTE: keep PYTHON_VERSION stable across re-bootstraps — every strategy"
log "      venv references the exact interpreter path under"
log "      $PYENV_ROOT/versions/<version>, so do not prune old versions."
log "Next: push to main (the CI deploy installs the code, writes the systemd"
log "      unit, installs the Python dependencies and restarts the service)."
log "Remember: cron jobs do NOT inherit the service environment — give the"
log "      cron-fired runner the same PATH (incl. $PYENV_ROOT/shims) and"
log "      PLATFORM_ROOT/STRATEGIES_DIR/S3_BUCKET/ENVIRONMENT."
