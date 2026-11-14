"""User accounts: signup, signin and the ownership rules.

The mobile number is the username; the password is stored only as a salted
PBKDF2 hash. Users live in the same S3 bucket as the strategies
(``users/{mobile}.json``) — no new data store (golden rule 1).
"""
from __future__ import annotations

import datetime as _dt
import secrets
from typing import List, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import (
    hash_password,
    issue_token,
    verify_password,
)
from app.models.user import (
    ACTIVE,
    BLOCKED,
    AuthResponse,
    PublicUser,
    SigninRequest,
    SignupRequest,
    UserRecord,
    UserStatus,
)
from app.storage.user_store import UserExistsError, UserNotFoundError, UserStore

log = get_logger("services.user")


class UserServiceError(Exception):
    pass


class AuthError(UserServiceError):
    """Bad credentials / duplicate account."""

class UserService:
    def __init__(self, settings: Optional[Settings] = None, store: Optional[UserStore] = None):
        self.settings = settings or get_settings()
        self.store = store or UserStore(self.settings)

    # --- accounts -----------------------------------------------------------
    def signup(self, req: SignupRequest) -> AuthResponse:
        mobile = (req.mobile or "").strip()
        full_name = (req.full_name or "").strip()
        if not mobile:
            raise AuthError("mobile number is required")
        if not full_name:
            raise AuthError("full name is required")
        if not req.password:
            raise AuthError("password is required")
        if self.store.exists(mobile):
            raise AuthError("an account with this mobile number already exists")
        password_hash, salt = hash_password(req.password)
        user = UserRecord(
            user_id=f"USR-{secrets.token_hex(4).upper()}",
            full_name=full_name,
            mobile=mobile,
            password_hash=password_hash,
            salt=salt,
            iterations=200_000,
            created_at=_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        )
        try:
            self.store.create(user)
        except UserExistsError as exc:
            raise AuthError("an account with this mobile number already exists") from exc
        log.info("user %s signed up", user.user_id)
        return self._auth_response(user)

    def signin(self, req: SigninRequest) -> AuthResponse:
        mobile = (req.mobile or "").strip()
        try:
            user = self.store.get_by_mobile(mobile)
        except Exception as exc:  # noqa: BLE001 — unknown mobile (any store error)
            # Same message for an unknown mobile and a wrong password.
            raise AuthError("invalid mobile number or password") from exc
        if not verify_password(req.password or "", user.password_hash, user.salt, user.iterations):
            raise AuthError("invalid mobile number or password")
        if user.status == BLOCKED:
            # A suspended account cannot sign in, even with the right password.
            raise AuthError("this account has been blocked by the administrator")
        user.last_login_at = (_dt.datetime.now(_dt.timezone.utc)
                               .replace(microsecond=0).isoformat())
        try:
            self.store.update(user)
        except UserNotFoundError:
            pass
        return self._auth_response(user)

    def get_by_mobile(self, mobile: str) -> Optional[UserRecord]:
        try:
            return self.store.get_by_mobile(mobile)
        except UserNotFoundError:
            return None

    def get_by_id(self, user_id: str) -> Optional[UserRecord]:
        for user in self.store.list_users():
            if user.user_id == user_id:
                return user
        return None

    # --- account status -----------------------------------------------------
    def set_status(self, user_id: str, new_status: UserStatus,
                   actor: str = "operator") -> UserRecord:
        """Block or unblock an account (operator action, audited).

        Blocking makes the account unusable at once: a sign-in is refused and
        every session token the account holds stops verifying (the token check
        re-reads the record, golden rule 1 — S3 is the source of truth).
        """
        if new_status not in (ACTIVE, BLOCKED):
            raise AuthError(f"unknown account status: {new_status}")
        record = self.get_by_id(user_id)
        if record is None:
            raise AuthError(f"no user with id {user_id}")
        now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
        record.status = new_status
        if new_status == BLOCKED:
            record.blocked_at = now
            record.blocked_by = actor
        else:
            record.blocked_at = None
            record.blocked_by = None
        try:
            self.store.update(record)
        except UserNotFoundError:
            raise AuthError(f"no user with id {user_id}")
        log.info("ADMIN set user %s status=%s by %s", user_id, new_status, actor)
        return record

    def block(self, user_id: str, actor: str = "operator") -> UserRecord:
        return self.set_status(user_id, BLOCKED, actor)

    def unblock(self, user_id: str, actor: str = "operator") -> UserRecord:
        return self.set_status(user_id, ACTIVE, actor)

    def list_users(self) -> List[UserRecord]:
        return self.store.list_users()

    # --- helpers ------------------------------------------------------------
    def _auth_response(self, user: UserRecord) -> AuthResponse:
        token = issue_token(user.user_id, user.mobile, self.settings,
                            ttl_seconds=self.settings.auth_token_ttl_seconds)
        return AuthResponse(token=token, user=PublicUser(
            user_id=user.user_id, full_name=user.full_name, mobile=user.mobile))
