"""User accounts and authentication payloads.

Users live in the same S3 bucket as the strategies (``users/{mobile}.json``) —
no new data store (golden rule 1). The mobile number is the username; the
password is stored as a salted PBKDF2-HMAC-SHA256 hash (never in clear text).
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field

from app.models.common import ApiModel


class UserRecord(ApiModel):
    """The stored user document."""

    user_id: str
    full_name: str
    mobile: str
    password_hash: str
    salt: str
    iterations: int = 200_000
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None


class PublicUser(ApiModel):
    """What the API returns about a user (never the hash)."""

    user_id: str
    full_name: str
    mobile: str


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
