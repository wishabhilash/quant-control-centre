"""Security helpers: authentication, authorisation, audit logging.

This is a deliberately compact security layer. In production the platform runs
under an EC2 IAM role (no static AWS credentials) and serves over HTTPS with
cookie/session auth + CSRF. In development a static API token may be used when
``AUTH_ENABLED`` is set. The design is intentionally minimal and documented; a
full RBAC/secret-management integration is a Phase 6 hardening task.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)


class SecurityError(HTTPException):
    def __init__(self, detail: str, status_code: int = status.HTTP_403_FORBIDDEN):
        super().__init__(status_code=status_code, detail=detail)


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
    if settings.dev_api_token is None:
        raise SecurityError("no admin credential configured", status.HTTP_500_INTERNAL_SERVER_ERROR)
    if credentials.credentials != settings.dev_api_token:
        raise SecurityError("invalid credential", status.HTTP_401_UNAUTHORIZED)


def ensure_production_https(request: Request, settings: Settings = Depends(get_settings)) -> None:
    if settings.is_production and settings.https_required and not request.url.scheme.startswith("https"):
        raise SecurityError("HTTPS required in production", status.HTTP_503_SERVICE_UNAVAILABLE)
