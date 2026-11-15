"""User accounts and authentication payloads.

Users live in the same S3 bucket as the strategies (``users/{mobile}.json``) —
no new data store (golden rule 1). The mobile number is the username; the
password is stored as a salted PBKDF2-HMAC-SHA256 hash (never in clear text).
"""
from __future__ import annotations

from typing import Optional, Literal

from app.models.common import ApiModel

# An account is either usable or suspended by the operator. A blocked account
# cannot sign in and any session token it holds stops working immediately.
UserStatus = Literal["active", "blocked"]
ACTIVE: UserStatus = "active"
BLOCKED: UserStatus = "blocked"


class UserRecord(ApiModel):
    """The stored user document."""

    user_id: str
    full_name: str
    mobile: str
    password_hash: str
    salt: str
    iterations: int = 200_000
    status: UserStatus = ACTIVE
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None
    blocked_at: Optional[str] = None
    blocked_by: Optional[str] = None
    # When a password was last changed/reset. Every session token issued *before*
    # this instant stops verifying (the token check compares its ``iat``), so a
    # change signs out every other device at once — no waiting for expiry.
    # None until the first change (old documents round-trip fine).
    password_changed_at: Optional[str] = None


class PublicUser(ApiModel):
    """What the API returns about a user (never the hash)."""

    user_id: str
    full_name: str
    mobile: str


class AccountInfo(PublicUser):
    """A user's own account, as shown in Account Settings (never the hash)."""

    status: UserStatus = ACTIVE
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None
    strategy_count: int = 0


class AdminUser(PublicUser):
    """A user as seen by the operator (administrative view)."""

    status: UserStatus = ACTIVE
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None
    blocked_at: Optional[str] = None
    blocked_by: Optional[str] = None
    strategy_count: int = 0


class SignupRequest(ApiModel):
    full_name: str
    mobile: str
    password: str


class SigninRequest(ApiModel):
    mobile: str
    password: str


class ChangePasswordRequest(ApiModel):
    """Self service: the user supplies their current password (the proof) plus the new one."""

    current_password: str
    new_password: str


class ResetPasswordRequest(ApiModel):
    """Operator reset: the operator sets a new password (they don't know the old one)."""

    new_password: str


class PasswordResetResponse(ApiModel):
    """The operator's one-time disclosure of the new password (hand it to the user)."""

    user: AdminUser
    new_password: str


class AuthResponse(ApiModel):
    token: str
    user: PublicUser
