#!/usr/bin/env bash
# Shared provisioning library: toolchain + pyenv + the platform's Python.
#
# Sourced by deploy/deploy-ec2.sh (which provisions on demand) and
# deploy/bootstrap-ec2.sh (the explicit one-time run). Both are idempotent:
# every step checks the current state before changing anything.
#
# Requires root (package installs, /opt, /etc/profile.d).

# --- configuration (override via the environment) ---------------------------
# PYTHON_VERSION      → the platform's own interpreter (FastAPI, uvicorn, boto3)
# STRATEGY_PYTHON_VERSION → the interpreter used for per-strategy venvs. It
#   defaults to the same version: broker SDKs (fyers-apiv3, dhanhq) pin old
#   dependencies whose wheels only exist for the older Python releases, so a
#   strategy venv on the newest Python would compile aiohttp from source
#   (slow, often failing). Set it separately only if you really need two.
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
STRATEGY_PYTHON_VERSION="${STRATEGY_PYTHON_VERSION:-$PYTHON_VERSION}"
PYENV_ROOT="${PYENV_ROOT:-/opt/pyenv}"
PYENV_INSTALLER_URL="${PYENV_INSTALLER_URL:-https://pyenv.run}"

# Build toolchain for compiling CPython and for wheels without a prebuilt
# binary (TA-Lib, pandas sdists, …) + the prerequisites of the pyenv installer
# itself (git, curl, tar, unzip).
PY_BUILD_PACKAGES=(gcc gcc-c++ make openssl-devel bzip2-devel libffi-devel
                   zlib-devel readline-devel sqlite-devel xz-devel ncurses-devel
                   gdbm-devel tk-devel
                   git curl tar unzip findutils patch which)

# Names that differ between distributions (AL2023 has libuuid-devel, older
# RHEL/Fedora has uuidlib-devel). They are optional — a missing one must not
# abort the provisioning.
PY_BUILD_OPTIONAL_PACKAGES=(libuuid-devel uuidlib-devel)

# --- helpers ----------------------------------------------------------------
# pyenv lists EXACT versions ("3.12.16"), never the minor ("3.12") — so the
# detection must match on the major.minor prefix, otherwise an installed
# version looks missing and gets rebuilt.
python_minor_installed() {
  local want="$1"
  [ -n "$want" ] || return 1
  "$PYENV_ROOT/bin/pyenv" versions --bare 2>/dev/null \
    | awk -F. 'NF >= 2 {print $1"."$2}' | grep -qx "$want"
}

# The newest installed build of a minor version (3.12 → 3.12.16).
latest_python_for_minor() {
  local want="$1"
  [ -n "$want" ] || return 1
  "$PYENV_ROOT/bin/pyenv" versions --bare 2>/dev/null \
    | grep -E "^${want//./\\.}\." | sort -V | tail -1
}

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
  # A bulk install is one round trip, but a single package name that does not
  # exist on this distribution aborts the whole transaction — so fall back to
  # installing the packages one by one and report the unavailable ones.
  if ! "$pkg" -y install "${PY_BUILD_PACKAGES[@]}" >/dev/null 2>&1; then
    echo "[python] bulk install failed — installing the packages individually"
    local p unavailable=()
    for p in "${PY_BUILD_PACKAGES[@]}"; do
      if ! "$pkg" -y install "$p" >/dev/null 2>&1; then
        unavailable+=("$p")
      fi
    done
    if [ ${#unavailable[@]} -gt 0 ]; then
      echo "[python] WARNING: unavailable on this distribution (ignored): ${unavailable[*]}"
    fi
  fi
  # Optional / distribution-specific names — never fatal.
  for p in "${PY_BUILD_OPTIONAL_PACKAGES[@]}"; do
    "$pkg" -y install "$p" >/dev/null 2>&1 || true
  done
  # The essentials must be there, otherwise the CPython build cannot work.
  local missing=()
  command -v gcc >/dev/null 2>&1 || missing+=("gcc")
  command -v make >/dev/null 2>&1 || missing+=("make")
  [ -e /usr/include/openssl/ssl.h ] || missing+=("openssl-devel")
  if [ ${#missing[@]} -gt 0 ]; then
    echo "[python] ERROR: missing build prerequisites: ${missing[*]}" >&2
    return 1
  fi
  echo "[python] build toolchain ready"
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
  local version seen=""
  for version in "$PYTHON_VERSION" "$STRATEGY_PYTHON_VERSION"; do
    [ -n "$version" ] || continue
    # The strategy version is usually the same as the platform's — install once.
    case " $seen " in *" $version "*) continue ;; esac
    seen="$seen $version"
    if python_minor_installed "$version"; then
      echo "[python] Python $version already installed ($(latest_python_for_minor "$version"))"
    else
      echo "[python] installing Python $version (compiling from source — a few minutes)"
      "$PYENV_ROOT/bin/pyenv" install --skip-existing "$version"
    fi
  done
  # The platform's interpreter is the default for every shell. Set the exact
  # installed build (pyenv also resolves a minor prefix, but being explicit
  # keeps `pyenv version` unambiguous).
  local global_version
  global_version="$(latest_python_for_minor "$PYTHON_VERSION")"
  [ -n "$global_version" ] || global_version="$PYTHON_VERSION"
  "$PYENV_ROOT/bin/pyenv" global "$global_version"
  "$PYENV_ROOT/bin/pyenv" rehash
  echo "[python] default Python: $global_version"
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

# The interpreter used for the per-strategy venvs (see STRATEGY_PYTHON_VERSION).
strategy_python_interpreter() {
  local candidate
  for candidate in "python${STRATEGY_PYTHON_VERSION}" python3.12 python3.11 python3; do
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
