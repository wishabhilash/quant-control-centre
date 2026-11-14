"""User document store.

One JSON document per user at ``users/{mobile}.json`` in the SAME S3 bucket as
the strategies — the mobile number is the username, so a sign-in is a single
object read (no scan). The documents carry a salted PBKDF2 password hash; the
plain password never leaves the request.
"""
from __future__ import annotations

from typing import List, Optional

from app.core.logging import get_logger
from app.models.user import UserRecord
from app.storage.s3 import S3Error, S3Storage
from app.core.config import get_settings

log = get_logger("storage.user_store")

PREFIX = "users/"


def key_for(mobile: str) -> str:
    return f"{PREFIX}{mobile}.json"


class UserNotFoundError(S3Error):
    pass


class UserExistsError(S3Error):
    pass


class UserStore:
    def __init__(self, storage: Optional[S3Storage] = None):
        self.storage = storage or S3Storage(get_settings())

    # --- CRUD ---------------------------------------------------------------
    def get_by_mobile(self, mobile: str) -> UserRecord:
        raw = self.storage.get_object(key_for(mobile))
        return UserRecord.model_validate_json(raw.decode("utf-8"))

    def exists(self, mobile: str) -> bool:
        return self.storage.get_etag(key_for(mobile)) is not None

    def create(self, user: UserRecord) -> UserRecord:
        key = key_for(user.mobile)
        if self.storage.get_etag(key):
            raise UserExistsError(f"user already exists: {user.mobile}")
        body = user.model_dump_json(indent=2).encode("utf-8")
        etag = self.storage.put_object(key, body)
        log.info("created user document %s etag=%s", user.user_id, etag)
        return user

    def update(self, user: UserRecord) -> UserRecord:
        key = key_for(user.mobile)
        if not self.storage.get_etag(key):
            raise UserNotFoundError(f"user not found: {user.mobile}")
        body = user.model_dump_json(indent=2).encode("utf-8")
        self.storage.put_object(key, body)
        return user

    def list_users(self) -> List[UserRecord]:
        out: List[UserRecord] = []
        for obj in self.storage.list_objects(prefix=PREFIX):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            raw = self.storage.get_object(key)
            try:
                out.append(UserRecord.model_validate_json(raw.decode("utf-8")))
            except Exception:  # noqa: BLE001 — skip an unreadable document
                continue
        return out
