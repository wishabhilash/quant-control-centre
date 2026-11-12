"""Strategy document and API DTO models (spec sections 5, 6, 7, 9)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.common import ApiModel, SCHEMA_VERSION

# 5 or 6 field cron (6 = seconds field).
_CRON_FIELD = re.compile(r"^(\*|\d+(-\d+)?)(,\d+(-\d+)?)*/?\d*(\*\d+)?$|^\*$")
_CRON_RE = re.compile(r"^(\S+\s+){4,5}\S+$")
_SCHEDULE_ID_RE = re.compile(r"^SCH-[A-Za-z0-9]+$")
_STRATEGY_ID_RE = re.compile(r"^STR-[A-Za-z0-9]+$")


class CodeBlock(ApiModel):
    encoding: str = "base64"
    entrypoint: str
    files: Dict[str, str] = Field(default_factory=dict)

    @field_validator("encoding")
    @classmethod
    def _check_encoding(cls, v: str) -> str:
        if v != "base64":
            raise ValueError("only base64 encoding is supported")
        return v

    @model_validator(mode="after")
    def _check_entrypoint(self) -> "CodeBlock":
        if self.entrypoint and self.entrypoint not in self.files:
            raise ValueError("entrypoint must reference a file present in the project")
        if self.entrypoint and not self.entrypoint.endswith(".py"):
            raise ValueError("entrypoint must be a Python (.py) file")
        return self


class Configuration(ApiModel):
    initial_capital: float = Field(default=100000.0)
    parameters: Dict[str, Any] = Field(default_factory=dict)


class RuntimeSettings(ApiModel):
    python_version: str = Field(default="3.12")
    timeout_seconds: int = Field(default=3600, ge=1, le=86400 * 7)
    allow_concurrent_runs: bool = Field(default=False)


class Schedule(ApiModel):
    schedule_id: str = Field(..., pattern=r"^SCH-[A-Za-z0-9]+$")
    name: str
    cron_expression: str
    timezone: str = Field(default="Asia/Kolkata")
    enabled: bool = True

    @field_validator("cron_expression")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        if not _CRON_RE.match(v.strip()):
            raise ValueError("invalid cron expression (expected 5 or 6 fields)")
        return v.strip()

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        try:
            import zoneinfo

            zoneinfo.ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid timezone: {v}") from exc
        return v


class DeploymentStatus(ApiModel):
    status: str = Field(default="not_deployed")  # not_deployed|deployed|failed
    last_deployed_at: Optional[str] = None
    source_hash: Optional[str] = None


class StrategyDocument(ApiModel):
    """The canonical per-strategy JSON document stored in S3."""

    schema_version: int = SCHEMA_VERSION
    strategy_id: str = Field(..., pattern=r"^STR-[A-Za-z0-9]+$")
    name: str
    description: str = ""
    enabled: bool = True
    code: CodeBlock
    configuration: Configuration = Field(default_factory=Configuration)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    schedules: List[Schedule] = Field(default_factory=list)
    deployment: DeploymentStatus = Field(default_factory=DeploymentStatus)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @model_validator(mode="after")
    def _unique_schedule_ids(self) -> "StrategyDocument":
        ids = [s.schedule_id for s in self.schedules]
        if len(ids) != len(set(ids)):
            raise ValueError("schedule_id values must be unique within a strategy")
        return self


# --- API DTOs --------------------------------------------------------------
class StrategyCreate(ApiModel):
    name: str
    description: str = ""
    enabled: bool = True
    entrypoint: str = "main.py"
    initial_capital: float = 100000.0
    parameters: Dict[str, Any] = Field(default_factory=dict)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    # Optional initial files (relative path -> UTF-8 text). Base64 encoding is
    # performed by the backend.
    files: Dict[str, str] = Field(default_factory=dict)


class StrategyUpdate(ApiModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    runtime: Optional[RuntimeSettings] = None


class CodeUpdate(ApiModel):
    """Full project save (spec section 8). Relative path -> UTF-8 text."""

    entrypoint: str
    files: Dict[str, str] = Field(default_factory=dict)
    # Optional expected document ETag for conditional writes.
    if_match_etag: Optional[str] = None


class ScheduleCreate(ApiModel):
    name: str
    cron_expression: str
    timezone: str = "Asia/Kolkata"
    enabled: bool = True

    @field_validator("cron_expression")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        if not _CRON_RE.match(v.strip()):
            raise ValueError("invalid cron expression")
        return v.strip()


class ScheduleUpdate(ApiModel):
    name: Optional[str] = None
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None
    enabled: Optional[bool] = None

    @field_validator("cron_expression")
    @classmethod
    def _check_cron(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _CRON_RE.match(v.strip()):
            raise ValueError("invalid cron expression")
        return v.strip()


class StrategySummary(ApiModel):
    strategy_id: str
    name: str
    description: str
    enabled: bool
    deployment_status: str
    schedule_count: int
    next_scheduled_execution: Optional[str] = None
    last_deployed_at: Optional[str] = None
    updated_at: Optional[str] = None


class StrategyListResponse(ApiModel):
    strategies: List[StrategySummary]
    count: int
