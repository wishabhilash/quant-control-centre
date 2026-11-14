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


class PublicUser(ApiModel):
    """What the API returns about a user (never the hash)."""

    user_id: str
    full_name: str
    mobile: str


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


class AuthResponse(ApiModel):
    token: str
    user: PublicUser
