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

# The entry point is fixed and NOT user-configurable: every strategy ships a
# ``main.py`` that the generic runner invokes. A per-schedule ``command`` supplies
# the arguments passed to it (see Schedule.command). The platform is otherwise
# agnostic about the rest of the project (golden rule 2).
DEFAULT_ENTRYPOINT = "main.py"


class CodeBlock(ApiModel):
    encoding: str = "base64"
    files: Dict[str, str] = Field(default_factory=dict)

    @field_validator("encoding")
    @classmethod
    def _check_encoding(cls, v: str) -> str:
        if v != "base64":
            raise ValueError("only base64 encoding is supported")
        return v


class Configuration(ApiModel):
    parameters: Dict[str, Any] = Field(default_factory=dict)


class Schedule(ApiModel):
    schedule_id: str = Field(..., pattern=r"^SCH-[A-Za-z0-9]+$")
    name: str
    cron_expression: str
    timezone: str = Field(default="Asia/Kolkata")
    enabled: bool = True
    # Arguments passed to the entry point when this schedule fires. Kept as a
    # single string; the runner splits it into an argument array (no shell) and
    # appends it to the invocation of the fixed entry point (golden rule 6).
    command: str = Field(default="")

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
    schedules: List[Schedule] = Field(default_factory=list)
    deployment: DeploymentStatus = Field(default_factory=DeploymentStatus)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _strip_deprecated_fields(cls, data: Any) -> Any:
        """Drop fields that were de-provisioned after the schema change.

        The entry point is now fixed to ``main.py`` (no longer user-settable),
        and ``runtime`` (python_version / timeout / concurrency) plus
        ``configuration.initial_capital`` are sourced from platform settings
        instead of the document. Because the model forbids extras
        (``extra='forbid'``), a previously-saved S3 document must still load:
        strip the legacy keys here so it validates (spec sections 1, 5).
        """
        if not isinstance(data, dict):
            return data
        code = data.get("code")
        if isinstance(code, dict) and "entrypoint" in code:
            code.pop("entrypoint", None)
        data.pop("runtime", None)
        cfg = data.get("configuration")
        if isinstance(cfg, dict):
            cfg.pop("initial_capital", None)
        return data

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
    parameters: Dict[str, Any] = Field(default_factory=dict)
    # Optional initial files (relative path -> UTF-8 text). Base64 encoding is
    # performed by the backend. The entry point is fixed to main.py.
    files: Dict[str, str] = Field(default_factory=dict)


class StrategyUpdate(ApiModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    # Configuration (parameters) can be updated in place so a full-document Save
    # persists it. The S3 schema already carries ``configuration``; this only
    # opens it for update (spec 5, 8).
    configuration: Optional[Configuration] = None


class CodeUpdate(ApiModel):
    """Full project save (spec section 8). Relative path -> UTF-8 text.

    The entry point is fixed to ``main.py`` (DEFAULT_ENTRYPOINT), so the client
    no longer sends one.
    """

    files: Dict[str, str] = Field(default_factory=dict)
    # Optional expected document ETag for conditional writes.
    if_match_etag: Optional[str] = None


class ScheduleCreate(ApiModel):
    name: str
    cron_expression: str
    timezone: str = "Asia/Kolkata"
    enabled: bool = True
    command: str = ""

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
    command: Optional[str] = None

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
