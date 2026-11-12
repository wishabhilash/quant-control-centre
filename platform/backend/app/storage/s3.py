"""S3 storage abstraction.

Uses the standard Boto3 interface. In development ``S3_ENDPOINT_URL`` points at a
local S3-compatible store (LocalStack by default) so the *exact same* code path is
exercised as production. No production strategy documents are ever accessed by
default (separate bucket). LocalStack (or any S3-compatible endpoint) needs path-style addressing, which is
applied automatically for any custom endpoint.
"""
from __future__ import annotations

from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger("storage.s3")


class S3Error(Exception):
    """Raised when an S3 operation fails."""


class S3Storage:
    """Thin wrapper over a Boto3 S3 client (AWS S3 / LocalStack)."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.bucket = self.settings.s3_bucket
        self.region = self.settings.region
        self._client = self._build_client()

    def _build_client(self):
        from botocore.config import Config

        s = self.settings
        kwargs: dict[str, Any] = {"region_name": s.region}
        # LocalStack / any custom endpoint requires path-style addressing;
        # real AWS S3 uses virtual-host style. "auto" resolves to "path" when a
        # custom endpoint is configured and "virtual" otherwise.
        style = s.s3_addressing_style
        if style == "auto":
            style = "path" if s.s3_endpoint_url else "virtual"
        if style in {"path", "virtual"}:
            kwargs["config"] = Config(s3={"addressing_style": style})
        # In development against LocalStack we fall back to LocalStack's default
        # credentials so the code path works out of the box; production uses the
        # EC2 IAM role (no static credentials).
        if s.is_local:
            kwargs["aws_access_key_id"] = s.aws_access_key_id or "localstack"
            kwargs["aws_secret_access_key"] = s.aws_secret_access_key or "testpass"
            if s.s3_endpoint_url:
                kwargs["endpoint_url"] = s.s3_endpoint_url
                kwargs["use_ssl"] = s.s3_use_ssl
        log.info("S3 client configured bucket=%s region=%s endpoint=%s style=%s",
                 s.s3_bucket, s.region, s.s3_endpoint_url or "(AWS)", style)
        return boto3.client("s3", **kwargs)

    # --- object operations -------------------------------------------------
    def put_object(self, key: str, body: bytes, content_type: str = "application/json",
                   if_match_etag: Optional[str] = None) -> str:
        """Store an object. Returns the new ETag. Honours conditional writes."""
        try:
            params: dict[str, Any] = {
                "Bucket": self.bucket, "Key": key, "Body": body,
                "ContentType": content_type,
            }
            # Server-side encryption by default (spec section 5).
            params["ServerSideEncryption"] = "AES256"
            # ETag-based conditional write prevents silent overwrite.
            if if_match_etag:
                params["IfMatch"] = if_match_etag
            resp = self._client.put_object(**params)
            return str(resp.get("ETag", "")).strip('"')
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "PreconditionFailed":
                raise S3Error("concurrent modification: ETag mismatch") from exc
            raise S3Error(f"put_object failed for {key}: {exc}") from exc

    def get_object(self, key: str, if_none_match_etag: Optional[str] = None) -> bytes:
        try:
            params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
            if if_none_match_etag:
                params["IfNoneMatch"] = if_none_match_etag
            resp = self._client.get_object(**params)
            return resp["Body"].read()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "NotFound"}:
                raise S3Error(f"object not found: {key}") from exc
            if code in {"PreconditionFailed", "304"}:
                raise S3Error("not modified") from exc
            raise S3Error(f"get_object failed for {key}: {exc}") from exc

    def head_object(self, key: str) -> dict:
        try:
            return self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise S3Error(f"object not found: {key}") from exc
            raise S3Error(f"head_object failed for {key}: {exc}") from exc

    def get_etag(self, key: str) -> Optional[str]:
        try:
            meta = self.head_object(key)
            return meta.get("ETag")
        except S3Error:
            return None

    def delete_object(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            raise S3Error(f"delete_object failed for {key}: {exc}") from exc

    def list_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            out.extend(page.get("Contents", []))
        return out

    def check_connectivity(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return True
        except ClientError as exc:  # head_bucket may 404 if bucket missing
            code = exc.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchBucket", "NotFound"}:
                return False
            return False
        except (NoCredentialsError, Exception):  # noqa: BLE001
            return False
