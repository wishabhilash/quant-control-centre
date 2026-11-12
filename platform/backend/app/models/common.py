"""Shared models and helpers."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = 1


class ApiModel(BaseModel):
    """Base model: camelCase-compatible, strict-ish, ignores extras where noted."""

    model_config = ConfigDict(extra="forbid")


class TimestampMixin(BaseModel):
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
