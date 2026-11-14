"""Application configuration.

All settings are sourced from environment variables so the same storage
abstraction and Boto3 interface works identically in development (LocalStack)
and production (AWS S3). See the spec sections 16 and 17.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the platform backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Environment -------------------------------------------------------
    environment: str = Field(default="development")
    region: str = Field(default="ap-south-1", alias="AWS_REGION")
    s3_bucket: str = Field(default="quant-platform-dev", alias="S3_BUCKET")
    s3_endpoint_url: Optional[str] = Field(default=None, alias="S3_ENDPOINT_URL")
    # Only meaningful for a custom endpoint (LocalStack). In production the
    # client talks to real AWS over TLS and this flag is not applied.
    s3_use_ssl: bool = Field(default=True, alias="S3_USE_SSL")
    # "auto" => path-style for a custom endpoint (LocalStack),
    # virtual-host for real AWS S3. LocalStack requires path-style addressing.
    s3_addressing_style: str = Field(default="auto", alias="S3_ADDRESSING_STYLE")
    # Optional static AWS credentials for local dev / LocalStack only.
    # Production uses the EC2 IAM role; these are intentionally absent.
    aws_access_key_id: Optional[str] = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: Optional[str] = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")

    # --- Platform filesystem layout (spec section 4) -----------------------
    platform_root: Path = Field(default=Path("/opt/quant"))
    strategies_dir: Optional[Path] = None
    runtime_dir: Optional[Path] = None
    locks_dir: Optional[Path] = None
    state_dir: Optional[Path] = None

    # --- Server / network --------------------------------------------------
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    api_base_path: str = Field(default="/api/v1")

    # --- Security ----------------------------------------------------------
    # Static dev credentials for local development only. In production an EC2
    # IAM role is used and auth is enforced via the security module.
    auth_enabled: bool = Field(default=False, alias="AUTH_ENABLED")
    dev_api_token: Optional[str] = Field(default=None, alias="DEV_API_TOKEN")
    allowed_hosts: List[str] = Field(default_factory=lambda: ["*"])
    https_required: bool = Field(default=False, alias="HTTPS_REQUIRED")

    # --- Runtime / process management --------------------------------------
    default_python_version: str = Field(default="3.12")
    default_run_timeout_seconds: int = Field(default=3600)
    process_grace_period_seconds: int = Field(default=30)
    allow_concurrent_runs: bool = Field(default=False)
    # Bound for a strategy's `pip install`: a dependency without a wheel for
    # the strategy's Python version falls back to a source build that can hang
    # for many minutes (or fail). The deploy fails with a clear message
    # instead.
    dependency_install_timeout_seconds: int = Field(default=900)

    # --- ZIP import limits -------------------------------------------------
    max_upload_bytes: int = Field(default=50 * 1024 * 1024)      # 50 MB
    max_upload_files: int = Field(default=5000)
    max_single_file_bytes: int = Field(default=10 * 1024 * 1024)  # 10 MB

    # --- Log handling ------------------------------------------------------
    log_level: str = Field(default="INFO")
    strategy_log_max_bytes: int = Field(default=10 * 1024 * 1024)  # 10 MB bounded log

    # --- Scheduler ---------------------------------------------------------
    default_timezone: str = Field(default="Asia/Kolkata")
    # When True the cron manager writes a platform-owned crontab file that is
    # not installed into the system crontab (used for local/macOS testing).
    scheduler_dry_run: bool = Field(default=True, alias="SCHEDULER_DRY_RUN")
    platform_cron_file: Optional[Path] = Field(
        default=None, alias="PLATFORM_CRON_FILE"
    )

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, v: str) -> str:
        v = v.lower()
        if v not in {"development", "production", "local", "test"}:
            raise ValueError("environment must be development, production, local or test")
        return v

    @field_validator("s3_addressing_style")
    @classmethod
    def _validate_addressing_style(cls, v: str) -> str:
        v = v.lower()
        if v not in {"auto", "path", "virtual"}:
            raise ValueError("s3_addressing_style must be auto, path or virtual")
        return v

    @model_validator(mode="after")
    def _validate_production_s3(self) -> "Settings":
        """Production talks to real AWS S3 over TLS — never a custom endpoint.

        A dev-style ``S3_ENDPOINT_URL`` (e.g. LocalStack) left in a production
        environment is a copy-paste accident: it is ignored by the S3 client
        (the endpoint is only applied in local environments), so it would
        silently mislead the operator. Fail fast instead.
        """
        if self.environment == "production" and (self.s3_endpoint_url or "").strip():
            raise ValueError(
                "S3_ENDPOINT_URL must be unset in production (real AWS S3 via the "
                "EC2 IAM role); remove it from the environment"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_local(self) -> bool:
        return self.environment in {"development", "local", "test"}

    def _resolve_dirs(self) -> None:
        """Compute concrete directory paths from platform_root if not explicit."""
        if self.strategies_dir is None:
            self.strategies_dir = self.platform_root / "strategies"
        if self.runtime_dir is None:
            self.runtime_dir = self.platform_root / "runtime"
        if self.locks_dir is None:
            self.locks_dir = self.runtime_dir / "locks"
        if self.state_dir is None:
            self.state_dir = self.runtime_dir / "state"

    def resolved_platform_root(self) -> Path:
        self._resolve_dirs()
        return self.platform_root

    def strategies_dir_path(self) -> Path:
        self._resolve_dirs()
        return self.strategies_dir

    def locks_dir_path(self) -> Path:
        self._resolve_dirs()
        return self.locks_dir

    def state_dir_path(self) -> Path:
        self._resolve_dirs()
        return self.state_dir

    def platform_cron_file_path(self) -> Path:
        self._resolve_dirs()
        if self.platform_cron_file is not None:
            return self.platform_cron_file
        return self.runtime_dir / "platform.cron"


@lru_cache
def get_settings() -> Settings:
    return Settings()
