"""Strategy document store.

One JSON document per strategy at ``strategies/{strategy_id}.json``. The store
handles base64 encoding/decoding of project files, SHA-256 source integrity
hashes and ETag-based conditional writes (spec sections 5, 8).
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
from typing import List, Optional

from app.core.logging import get_logger
from app.models.strategy import (
    CodeBlock,
    StrategyDocument,
)
from app.storage.s3 import S3Error, S3Storage
from app.core.config import get_settings

log = get_logger("storage.strategy_store")

PREFIX = "strategies/"


def utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def key_for(strategy_id: str) -> str:
    return f"{PREFIX}{strategy_id}.json"


class StrategyNotFoundError(S3Error):
    pass


class ConditionalWriteError(S3Error):
    pass


class StrategyStore:
    def __init__(self, storage: Optional[S3Storage] = None):
        self.storage = storage or S3Storage(get_settings())

    # --- base64 / hashing helpers -----------------------------------------
    @staticmethod
    def encode_file(text: str) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    @staticmethod
    def decode_file(b64: str) -> str:
        return base64.b64decode(b64.encode("ascii")).decode("utf-8")

    @staticmethod
    def hash_files(files: dict[str, str]) -> str:
        """Deterministic SHA-256 over (sorted path + content) pairs."""
        h = hashlib.sha256()
        for path in sorted(files):
            h.update(path.encode("utf-8"))
            h.update(b"\x00")
            h.update(files[path].encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def document_hash(self, doc: StrategyDocument) -> str:
        return self.hash_files(doc.code.files)

    # --- CRUD -------------------------------------------------------------
    def generate_strategy_id(self, existing: Optional[List[str]] = None) -> str:
        existing = set(existing or self._list_strategy_ids())
        n = 1
        while True:
            candidate = f"STR-{n:03d}"
            if candidate not in existing:
                return candidate
            n += 1

    def _list_strategy_ids(self) -> List[str]:
        out = []
        for obj in self.storage.list_objects(prefix=PREFIX):
            key = obj["Key"]
            if key.endswith(".json") and key != f"{PREFIX}index.json":
                out.append(key[len(PREFIX):-len(".json")])
        return out

    def list_documents(self) -> List[StrategyDocument]:
        docs = []
        for sid in self._list_strategy_ids():
            raw = self.storage.get_object(key_for(sid))
            docs.append(StrategyDocument.model_validate_json(raw.decode("utf-8")))
        return docs

    def get_document(self, strategy_id: str, if_none_match_etag: Optional[str] = None) -> StrategyDocument:
        raw = self.storage.get_object(key_for(strategy_id), if_none_match_etag=if_none_match_etag)
        return StrategyDocument.model_validate_json(raw.decode("utf-8"))

    def get_etag(self, strategy_id: str) -> Optional[str]:
        return self.storage.get_etag(key_for(strategy_id))

    def create_document(self, doc: StrategyDocument) -> StrategyDocument:
        key = key_for(doc.strategy_id)
        if self.storage.get_etag(key):
            raise S3Error(f"strategy already exists: {doc.strategy_id}")
        now = utcnow_iso()
        doc.created_at = now
        doc.updated_at = now
        doc.deployment.status = "not_deployed"
        doc.deployment.last_deployed_at = None
        doc.deployment.source_hash = None
        body = doc.model_dump_json(indent=2).encode("utf-8")
        etag = self.storage.put_object(key, body)
        log.info("created strategy document %s etag=%s", doc.strategy_id, etag)
        return doc

    def update_document(
        self,
        doc: StrategyDocument,
        if_match_etag: Optional[str] = None,
    ) -> StrategyDocument:
        """Conditional update. If ``if_match_etag`` is provided, the write fails
        (ConditionalWriteError) when the server ETag differs, protecting
        concurrent edits from silently overwriting one another."""
        key = key_for(doc.strategy_id)
        if not self.storage.get_etag(key):
            raise StrategyNotFoundError(f"strategy not found: {doc.strategy_id}")
        # Integrity check: recompute source hash.
        doc.deployment.source_hash = self.document_hash(doc)
        doc.updated_at = utcnow_iso()
        body = doc.model_dump_json(indent=2).encode("utf-8")
        try:
            etag = self.storage.put_object(key, body, if_match_etag=if_match_etag)
        except S3Error as exc:
            if "ETag mismatch" in str(exc) or "concurrent" in str(exc):
                raise ConditionalWriteError("concurrent modification: ETag mismatch") from exc
            raise
        log.info("updated strategy document %s etag=%s", doc.strategy_id, etag)
        return doc

    def delete_document(self, strategy_id: str) -> None:
        key = key_for(strategy_id)
        if not self.storage.get_etag(key):
            raise StrategyNotFoundError(f"strategy not found: {strategy_id}")
        self.storage.delete_object(key)
        log.info("deleted strategy document %s", strategy_id)

    def exists(self, strategy_id: str) -> bool:
        return self.storage.get_etag(key_for(strategy_id)) is not None
