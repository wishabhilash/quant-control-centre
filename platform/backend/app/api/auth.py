"""Authentication endpoints: signup, signin, whoami.

Public routes (no token required). Every other admin route needs a token —
either a user token from here or the operator's admin token.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_user_service
from app.core.config import Settings, get_settings
from app.core.security import Principal, get_current_user
from app.models.user import (
    AccountInfo,
    AuthResponse,
    ChangePasswordRequest,
    PublicUser,
    SigninRequest,
    SignupRequest,
)
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
def auth_status(settings: Settings = Depends(get_settings)) -> dict:
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


@router.get("/account", response_model=AccountInfo)
def account(principal: Principal = Depends(get_current_user),
            svc: UserService = Depends(get_user_service)) -> AccountInfo:
    """The signed-in user's own account (Account Settings) — never the hash.

    The operator break-glass token has no user account, so it gets a 404 here;
    the Account Settings surface is for real user sessions only.
    """
    try:
        return svc.account_info(principal.user_id)
    except AuthError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/change-password", response_model=AuthResponse)
def change_password(req: ChangePasswordRequest,
                    principal: Principal = Depends(get_current_user),
                    svc: UserService = Depends(get_user_service)) -> AuthResponse:
    """Swap the signed-in user's own password.

    The current password is the proof of identity (no email/SMS to verify
    otherwise). On success a *fresh* token is returned so this session
    survives while every other session for the account is retired. A wrong
    current password is a 400 (the request is malformed) — NOT a 401, which
    would kick the user to the sign-in screen.
    """
    try:
        return svc.change_password(principal.user_id, req.current_password,
                                   req.new_password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
