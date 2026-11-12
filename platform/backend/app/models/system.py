"""System and runtime status models (spec sections 12, 14)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.models.common import ApiModel

# Runtime process states (spec section 12).
RUNTIME_STATES = {"not_deployed", "stopped", "running", "failed", "disabled"}


class HealthResponse(ApiModel):
    status: str = "ok"
    environment: str
    version: str
    checks: Dict[str, str] = Field(default_factory=dict)


class StorageStatus(ApiModel):
    reachable: bool
    bucket: str
    region: str
    endpoint_url: Optional[str] = None
    detail: Optional[str] = None


class SchedulerStatus(ApiModel):
    active: bool
    mode: str  # "system_cron" | "dry_run"
    managed_schedule_count: int
    cron_file: Optional[str] = None
    detail: Optional[str] = None
    # Effective paths used in the generated cron lines, so the frontend can
    # preview the exact line (PLATFORM_ROOT-derived, spec §4).
    runner_python: Optional[str] = None
    runner_script: Optional[str] = None
    strategies_dir: Optional[str] = None


class ProcessInfo(ApiModel):
    strategy_id: str
    schedule_id: Optional[str] = None
    state: str
    pid: Optional[int] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    exit_code: Optional[int] = None


class RuntimeStatus(ApiModel):
    strategy_id: str
    state: str
    process: Optional[ProcessInfo] = None
    has_live_positions_guard: bool = False
    deployment_status: str = "not_deployed"
    deployed_source_hash: Optional[str] = None
    updated_at: Optional[str] = None
