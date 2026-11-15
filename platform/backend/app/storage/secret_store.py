"""AWS Secrets Manager wrapper for broker credentials.

Broker credentials (TOTP secret, client id, PIN) and the generated access
token live here — **never** in an S3 document or a strategy project (golden
rule 7). In production the client uses the EC2 IAM role (no static keys); in
local development it points at LocalStack like the S3 client does.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import boto3

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger("storage.secrets")


class SecretStoreError(Exception):
    """Raised when a secret cannot be stored, read or deleted."""


class SecretStore:
    """Thin Boto3 Secrets Manager wrapper (AWS / LocalStack)."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._client = self._build_client()

    def _build_client(self):
        s = self.settings
        kwargs: dict[str, Any] = {"region_name": s.region}
        if s.is_local:
            kwargs["aws_access_key_id"] = s.aws_access_key_id or "localstack"
            kwargs["aws_secret_access_key"] = s.aws_secret_access_key or "testpass"
            if s.s3_endpoint_url:
                kwargs["endpoint_url"] = s.s3_endpoint_url
                kwargs["use_ssl"] = s.s3_use_ssl
        return boto3.client("secretsmanager", **kwargs)

    def _name(self, key: str) -> str:
        """Full secret name for a platform-relative ``key`` (no leading slash)."""
        return f"{self.settings.secrets_prefix}/{key}".strip("/")

    # --- operations ---------------------------------------------------------
    def put_secret(self, key: str, payload: dict) -> None:
        """Create or update the secret ``key`` with a JSON payload."""
        name = self._name(key)
        body = json.dumps(payload)
        try:
            try:
                self._client.create_secret(Name=name, SecretString=body)
            except self._client.exceptions.ResourceExistsException:
                self._client.put_secret_value(SecretId=name, SecretString=body)
        except Exception as exc:  # noqa: BLE001 — mapped by the service layer
            raise SecretStoreError(f"could not store secret {key}: {exc}") from exc

    def get_secret(self, key: str) -> Optional[dict]:
        """The secret's JSON payload, or ``None`` when it does not exist."""
        name = self._name(key)
        try:
            value = self._client.get_secret_value(SecretId=name)
        except self._client.exceptions.ResourceNotFoundException:
            return None
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"could not read secret {key}: {exc}") from exc
        try:
            return json.loads(value.get("SecretString") or "{}")
        except ValueError as exc:
            raise SecretStoreError(f"secret {key} is not valid JSON") from exc

    def delete_secret(self, key: str) -> None:
        """Delete a secret immediately (no recovery window) so the same key
        can be re-created at once when the user re-enables the broker."""
        name = self._name(key)
        try:
            self._client.delete_secret(
                SecretId=name, ForceDeleteWithoutRecovery=True
            )
        except self._client.exceptions.ResourceNotFoundException:
            return  # already gone — disabling is idempotent
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"could not delete secret {key}: {exc}") from exc
