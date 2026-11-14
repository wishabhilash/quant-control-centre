"""Authentication endpoints: signup, signin, whoami.

Public routes (no token required). Every other admin route needs a token —
either a user token from here or the operator's admin token.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_user_service
from app.core.config import Settings, get_settings
from app.core.security import Principal, get_current_user
from app.models.user import AuthResponse, PublicUser, SigninRequest, SignupRequest
from app.services.user_service import AuthError, UserService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def signup(req: SignupRequest, svc: UserService = Depends(get_user_service)) -> AuthResponse:
    """Create an account. The mobile number is the username."""
    try:
        return svc.signup(req)
    except AuthError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.post("/signin", response_model=AuthResponse)
def signin(req: SigninRequest, svc: UserService = Depends(get_user_service)) -> AuthResponse:
    """Exchange mobile + password for a session token."""
    try:
        return svc.signin(req)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))


@router.get("/status")
def status(settings: Settings = Depends(get_settings)) -> dict:
    """Whether the deployment requires a session token.

    The dashboard uses it to skip the sign-in screen where authentication is
    disabled (local development) instead of demanding a token that is not
    needed.
    """
    return {"auth_enabled": settings.auth_enabled,
            "signup_enabled": settings.auth_enabled}


@router.get("/me", response_model=PublicUser)
def me(principal: Principal = Depends(get_current_user)) -> PublicUser:
    """The signed-in user (or the admin token's identity)."""
    return PublicUser(user_id=principal.user_id, full_name=principal.full_name,
                      mobile=principal.mobile)
