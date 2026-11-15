"""AWS Systems Manager **Parameter Store** wrapper for broker credentials.

Broker credentials (TOTP secret, client id, PIN) and the generated access
token live here — **never** in an S3 document or a strategy project (golden
rule 7). In production the client uses the EC2 IAM role (no static keys); in
local development it points at LocalStack like the S3 client does.

Why Parameter Store and not Secrets Manager: standard parameters — including
``SecureString`` ones — are **free**, while Secrets Manager bills per secret
per month. A ``SecureString`` parameter is encrypted with the AWS-managed
``aws/ssm`` KMS key (its usage through SSM is free and needs no explicit
``kms:`` permission). The values are tiny JSON documents, far below the 4 KB
standard-parameter limit.

The method names keep the "secret" vocabulary (``put_secret`` …): they read
naturally at the call sites and the *semantics* — store / fetch / remove a
sensitive value — are unchanged by the backend swap.
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
    """Thin Boto3 Parameter Store (SSM) wrapper (AWS / LocalStack)."""

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
        return boto3.client("ssm", **kwargs)

    def _name(self, key: str) -> str:
        """Full parameter name for a platform-relative ``key``.

        Always **slash-prefixed** (``/{prefix}/{key}``): Parameter Store's
        canonical form, which LocalStack requires for ``GetParameter``
        (a bare ``a/b`` name fails validation there) and real AWS accepts.
        """
        return "/" + f"{self.settings.secrets_prefix}/{key}".strip("/")

    # --- operations ---------------------------------------------------------
    def put_secret(self, key: str, payload: dict) -> None:
        """Create or update the parameter ``key`` with a JSON payload
        (``SecureString``, default ``aws/ssm`` key)."""
        name = self._name(key)
        body = json.dumps(payload)
        try:
            self._client.put_parameter(
                Name=name, Type="SecureString", Value=body, Overwrite=True
            )
        except Exception as exc:  # noqa: BLE001 — mapped by the service layer
            raise SecretStoreError(f"could not store secret {key}: {exc}") from exc

    def get_secret(self, key: str) -> Optional[dict]:
        """The parameter's JSON payload, or ``None`` when it does not exist."""
        name = self._name(key)
        try:
            value = self._client.get_parameter(Name=name, WithDecryption=True)
        except self._client.exceptions.ParameterNotFound:
            return None
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"could not read secret {key}: {exc}") from exc
        try:
            return json.loads(value["Parameter"].get("Value") or "{}")
        except ValueError as exc:
            raise SecretStoreError(f"secret {key} is not valid JSON") from exc

    def delete_secret(self, key: str) -> None:
        """Delete a parameter immediately (SSM has no recovery window), so the
        same key can be re-created at once when the user re-enables the
        broker."""
        name = self._name(key)
        try:
            self._client.delete_parameter(Name=name)
        except self._client.exceptions.ParameterNotFound:
            return  # already gone — disabling is idempotent
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"could not delete secret {key}: {exc}") from exc
