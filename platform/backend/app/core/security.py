"""Security helpers: user authentication, authorisation, audit logging.

This is a deliberately compact security layer. In production the platform runs
under an EC2 IAM role (no static AWS credentials) and serves over HTTPS with
cookie/session auth + CSRF. In development a static API token may be used when
``AUTH_ENABLED`` is set. The design is intentionally minimal and documented; a
full RBAC/secret-management integration is a Phase 6 hardening task.

Authentication has two accepted credentials:
  * a **user token** — issued by ``/auth/signup`` and ``/auth/signin``, a
    compact HMAC-signed token carrying the user id and the mobile number;
  * the **admin token** (``DEV_API_TOKEN``) — the operator's break-glass
    credential, which bypasses per-user ownership checks.

Passwords are never stored: they are hashed with PBKDF2-HMAC-SHA256 and a
per-user random salt.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)

# PBKDF2 work factor. High enough to make offline cracking expensive, low
# enough to keep a sign-in snappy on a small instance.
PBKDF2_ITERATIONS = 200_000
TOKEN_TTL_SECONDS = 12 * 3600  # 12 hours


class SecurityError(HTTPException):
    def __init__(self, detail: str, status_code: int = status.HTTP_403_FORBIDDEN):
        super().__init__(status_code=status_code, detail=detail)


# --- passwords --------------------------------------------------------------
def hash_password(password: str, salt: Optional[str] = None,
                   iterations: int = PBKDF2_ITERATIONS) -> tuple[str, str]:
    """Return ``(hash_hex, salt_hex)`` for a password."""
    if not password:
        raise ValueError("password must not be empty")
    salt_hex = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
    )
    return digest.hex(), salt_hex


def verify_password(password: str, password_hash: str, salt: str,
                    iterations: int = PBKDF2_ITERATIONS) -> bool:
    """Constant-time password check."""
    try:
        computed, _ = hash_password(password, salt, iterations)
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(computed, password_hash or "")


# --- tokens ----------------------------------------------------------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def signing_secret(settings: Settings) -> bytes:
    """The HMAC key for user tokens.

    ``AUTH_SIGNING_SECRET`` is the production setting. When it is not set a
    random per-process secret is used: tokens then stop working after a restart
    (and the platform logs a warning).
    """
    configured = getattr(settings, "auth_signing_secret", None)
    if configured:
        return configured.encode("utf-8")
    global _EPHEMERAL_SECRET
    if _EPHEMERAL_SECRET is None:
        _EPHEMERAL_SECRET = secrets.token_bytes(32)
    return _EPHEMERAL_SECRET


_EPHEMERAL_SECRET: Optional[bytes] = None


def issue_token(user_id: str, mobile: str, settings: Settings,
                ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
    """A compact signed token: ``base64url(payload).base64url(hmac)``."""
    payload = {"sub": user_id, "mob": mobile,
               "exp": int(time.time()) + int(ttl_seconds)}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(signing_secret(settings), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_token(token: str, settings: Settings) -> Optional[dict]:
    """Decode + verify a user token; ``None`` when invalid or expired."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64e(hmac.new(signing_secret(settings), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


# --- principals -------------------------------------------------------------
class Principal:
    """The authenticated caller: a user, or the admin break-glass token."""

    def __init__(self, user_id: str, mobile: str, full_name: str = "", is_admin: bool = False):
        self.user_id = user_id
        self.mobile = mobile
        self.full_name = full_name
        self.is_admin = is_admin

    def __repr__(self) -> str:  # pragma: no cover
        return f"Principal(user_id={self.user_id!r}, admin={self.is_admin})"


ADMIN_PRINCIPAL = Principal("admin", "admin", "Administrator", is_admin=True)


def _principal_from_token(token: str, settings: Settings, user_store) -> Optional[Principal]:
    # The admin token keeps working as a break-glass credential.
    if settings.auth_enabled and settings.dev_api_token and \
            hmac.compare_digest(token, settings.dev_api_token):
        return ADMIN_PRINCIPAL
    payload = decode_token(token, settings)
    if not payload:
        return None
    try:
        record = user_store.get_by_mobile(payload.get("mob", ""))
    except Exception:  # noqa: BLE001
        return None
    return Principal(record.user_id, record.mobile, record.full_name)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """FastAPI dependency: the authenticated principal (401 when missing).

    A presented token is always honoured — even where authentication is
    disabled — so a local developer can sign in as a user and exercise the
    per-user ownership rules. Without a token: admin when auth is disabled
    (development), 401 otherwise.
    """
    if credentials is not None and credentials.scheme.lower() == "bearer":
        from app.api.deps import get_user_store
        principal = _principal_from_token(credentials.credentials, settings, get_user_store())
        if principal is not None:
            return principal
    if not settings.auth_enabled:
        return ADMIN_PRINCIPAL
    raise SecurityError("authentication required", status.HTTP_401_UNAUTHORIZED)


async def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject unauthenticated administrative requests when auth is enabled."""
    if not settings.auth_enabled:
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise SecurityError("authentication required", status.HTTP_401_UNAUTHORIZED)
    if settings.dev_api_token and hmac.compare_digest(credentials.credentials, settings.dev_api_token):
        return
    if decode_token(credentials.credentials, settings) is None:
        raise SecurityError("invalid credential", status.HTTP_401_UNAUTHORIZED)


async def require_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    """Only the operator's break-glass token may use the admin routes.

    A user token is NOT enough — the operator's administrative surface must
    not be reachable by ordinary accounts.
    """
    if not settings.auth_enabled:
        return  # development: authentication is off, the local operator is trusted
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise SecurityError("operator credential required", status.HTTP_401_UNAUTHORIZED)
    if not settings.dev_api_token or not hmac.compare_digest(
        credentials.credentials, settings.dev_api_token
    ):
        raise SecurityError("operator credential required", status.HTTP_403_FORBIDDEN)


def ensure_production_https(request: Request, settings: Settings = Depends(get_settings)) -> None:
    if settings.is_production and settings.https_required and not request.url.scheme.startswith("https"):
        raise SecurityError("HTTPS required in production", status.HTTP_503_SERVICE_UNAVAILABLE)
