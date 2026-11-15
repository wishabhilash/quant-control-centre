"""Broker connection metadata store.

One JSON document per user per broker at ``brokers/{user_id}/{broker}.json`` in
the SAME S3 bucket as the strategies (golden rule 1). The documents hold only
**non-secret metadata** (status, timestamps, last error) — the credentials and
the access token live in Secrets Manager (see ``secret_store.py``).
"""
from __future__ import annotations

from typing import List, Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.broker import BrokerDocument
from app.storage.s3 import S3Error, S3Storage

log = get_logger("storage.broker_store")

PREFIX = "brokers/"


class BrokerStore:
    def __init__(self, storage: Optional[S3Storage] = None):
        self.storage = storage or S3Storage(get_settings())

    def _key(self, user_id: str, broker: str) -> str:
        return f"{PREFIX}{user_id}/{broker}.json"

    def get(self, user_id: str, broker: str) -> Optional[BrokerDocument]:
        try:
            raw = self.storage.get_object(self._key(user_id, broker))
        except S3Error:
            return None
        return BrokerDocument.model_validate_json(raw.decode("utf-8"))

    def put(self, doc: BrokerDocument) -> BrokerDocument:
        body = doc.model_dump_json(indent=2).encode("utf-8")
        self.storage.put_object(self._key(doc.user_id, doc.broker), body)
        return doc

    def delete(self, user_id: str, broker: str) -> None:
        try:
            self.storage.delete_object(self._key(user_id, broker))
        except S3Error:
            pass  # already gone — disabling is idempotent

    def list_for_user(self, user_id: str) -> List[BrokerDocument]:
        out: List[BrokerDocument] = []
        prefix = f"{PREFIX}{user_id}/"
        for obj in self.storage.list_objects(prefix=prefix):
            if not obj["Key"].endswith(".json"):
                continue
            try:
                raw = self.storage.get_object(obj["Key"])
                out.append(BrokerDocument.model_validate_json(raw.decode("utf-8")))
            except Exception:  # noqa: BLE001 — skip an unreadable document
                continue
        return out

    def list_all(self) -> List[BrokerDocument]:
        """Every connection on the platform (used by the startup reconcile)."""
        out: List[BrokerDocument] = []
        for obj in self.storage.list_objects(prefix=PREFIX):
            if not obj["Key"].endswith(".json"):
                continue
            try:
                raw = self.storage.get_object(obj["Key"])
                out.append(BrokerDocument.model_validate_json(raw.decode("utf-8")))
            except Exception:  # noqa: BLE001
                continue
        return out
