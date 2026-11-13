#!/usr/bin/env bash
# Shared provisioning library: toolchain + pyenv + the platform's Python.
#
# Sourced by deploy/deploy-ec2.sh (which provisions on demand) and
# deploy/bootstrap-ec2.sh (the explicit one-time run). Both are idempotent:
# every step checks the current state before changing anything.
#
# Requires root (package installs, /opt, /etc/profile.d).

# --- configuration (override via the environment) ---------------------------
PYTHON_VERSION="${PYTHON_VERSION:-3.14}"
PYENV_ROOT="${PYENV_ROOT:-/opt/pyenv}"
PYENV_INSTALLER_URL="${PYENV_INSTALLER_URL:-https://pyenv.run}"

# Build toolchain for compiling CPython and for wheels without a prebuilt
# binary (TA-Lib, pandas sdists, …) + the prerequisites of the pyenv installer
# itself (git, curl, tar, unzip).
PY_BUILD_PACKAGES=(gcc gcc-c++ make openssl-devel bzip2-devel libffi-devel
                   zlib-devel readline-devel sqlite-devel xz-devel ncurses-devel
                   gdbm-devel uuidlib-devel tk-devel
                   git curl tar unzip findutils patch which)

# --- helpers ----------------------------------------------------------------
_pkg_manager() {
  if command -v dnf >/dev/null 2>&1; then echo dnf
  elif command -v yum >/dev/null 2>&1; then echo yum
  else echo ""
  fi
}

ensure_build_toolchain() {
  local pkg
  pkg="$(_pkg_manager)"
  if [ -z "$pkg" ]; then
    echo "[python] ERROR: neither dnf nor yum found (Amazon Linux expected)" >&2
    return 1
  fi
  if command -v gcc >/dev/null 2>&1 && [ -e /usr/include/openssl/ssl.h ]; then
    echo "[python] build toolchain already present"
    return 0
  fi
  echo "[python] installing the build toolchain with $pkg"
  "$pkg" -y install "${PY_BUILD_PACKAGES[@]}"
}

ensure_pyenv() {
  if [ ! -d "$PYENV_ROOT/.git" ]; then
    echo "[python] installing pyenv into $PYENV_ROOT via the official installer"
    # Fetch the installer to a file first instead of piping curl into bash:
    # it can be inspected, and a failure leaves the script on disk. The
    # installer is pyenv's own wrapper around a git clone of pyenv/pyenv.
    local installer
    installer="$(mktemp)"
    if curl -fsSL -o "$installer" "$PYENV_INSTALLER_URL"; then
      PYENV_ROOT="$PYENV_ROOT" bash "$installer" || {
        echo "[python] installer failed — falling back to a plain git clone" >&2
        rm -f "$installer"
        git clone --depth 1 https://github.com/pyenv/pyenv.git "$PYENV_ROOT"
        return 0
      }
      rm -f "$installer"
    else
      echo "[python] could not download the installer — falling back to git clone" >&2
      git clone --depth 1 https://github.com/pyenv/pyenv.git "$PYENV_ROOT"
    fi
  else
    echo "[python] pyenv already installed at $PYENV_ROOT"
  fi
  export PYENV_ROOT
  export PATH="$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH"
  # Shared installation: every user (including the service account) may use it.
  chmod -R a+rX "$PYENV_ROOT"
}

ensure_python_version() {
  export PYENV_ROOT
  export PATH="$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH"
  if ! "$PYENV_ROOT/bin/pyenv" versions --bare 2>/dev/null | grep -qx "$PYTHON_VERSION"; then
    echo "[python] installing Python $PYTHON_VERSION (compiling from source — a few minutes)"
    "$PYENV_ROOT/bin/pyenv" install --skip-existing "$PYTHON_VERSION"
  else
    echo "[python] Python $PYTHON_VERSION already installed"
  fi
  # Make it the default for every shell.
  "$PYENV_ROOT/bin/pyenv" global "$PYTHON_VERSION"
  "$PYENV_ROOT/bin/pyenv" rehash
}

ensure_python_profile() {
  cat > /etc/profile.d/pyenv.sh <<EOF
# Managed by deploy/ — pyenv shims for all users.
export PYENV_ROOT="$PYENV_ROOT"
export PATH="\$PYENV_ROOT/shims:\$PYENV_ROOT/bin:\$PATH"
EOF
  chmod 644 /etc/profile.d/pyenv.sh
}

# Full provisioning: toolchain → pyenv → Python → default → profile.
ensure_python() {
  ensure_build_toolchain
  ensure_pyenv
  ensure_python_version
  ensure_python_profile
  export PYENV_ROOT
  export PATH="$PYENV_ROOT/shims:$PYENV_ROOT/bin:$PATH"
  local version
  version="$(pyenv exec python --version 2>&1 || echo unknown)"
  echo "[python] ready: $version (pyenv root $PYENV_ROOT, default $PYTHON_VERSION)"
}

# The interpreter to build venvs with: the pyenv shim for the requested
# version, falling back to whatever python3 is available.
python_interpreter() {
  if [ -n "${PYTHON_BIN:-}" ] && [ -x "${PYTHON_BIN:-}" ]; then
    echo "$PYTHON_BIN"; return 0
  fi
  local candidate
  for candidate in "python${PYTHON_VERSION}" python3.14 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"; return 0
    fi
  done
  return 1
}

# The platform installs its crontab entries into the SYSTEM crontab — without
# a cron implementation the schedules silently never fire. Amazon Linux 2023
# ships cronie (which also honours CRON_TZ); older AL images use vixie cron.
ensure_cron() {
  local pkg
  pkg="$(_pkg_manager)"
  if ! command -v crontab >/dev/null 2>&1; then
    [ -n "$pkg" ] || { echo "[cron] ERROR: no package manager available" >&2; return 1; }
    echo "[cron] installing cronie"
    "$pkg" -y install cronie || "$pkg" -y install cronie-anacron || true
  fi
  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now crond >/dev/null 2>&1 \
      || systemctl enable --now cron >/dev/null 2>&1 \
      || echo "[cron] WARNING: could not start the cron daemon" >&2
  fi
  command -v crontab >/dev/null 2>&1 \
    && echo "[cron] crontab available: $(command -v crontab)"
}
