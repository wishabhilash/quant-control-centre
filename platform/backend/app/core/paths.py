"""Filesystem path helpers and reserved-path protection.

The platform must never let a Monaco project overwrite or delete
platform-managed resources (``.venv/`` and ``strategy.log``). These helpers
centralise that policy and are used by the code, deployment and ZIP-import
services.
"""
from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import List, Sequence

# Platform-managed entries reserved inside every strategy project directory.
RESERVED_TOP_LEVEL = {".venv", "strategy.log", "strategy.pid"}

# Runtime-managed relative paths that must never appear in a user project.
RESERVED_RELATIVE_PREFIXES: Sequence[str] = (".venv/", "strategy.log", "strategy.pid")


def normalize_relative_path(path: str) -> str:
    """Normalise a user-supplied relative path.

    Returns a POSIX-style relative path with no ``..`` traversal and no
    absolute component. Raises ``ValueError`` for illegal paths.
    """
    if not path or not isinstance(path, str):
        raise ValueError("path must be a non-empty string")
    # Reject NUL bytes and absolute paths up front.
    if "\x00" in path:
        raise ValueError("path contains NUL byte")
    if path.startswith(("/", "\\")) or os.path.isabs(path):
        raise ValueError("absolute paths are not allowed")
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("path traversal ('..') is not allowed")
    normalised = "/".join(parts)
    if not normalised:
        raise ValueError("path is empty after normalisation")
    return normalised


def is_reserved(path: str) -> bool:
    """True if ``path`` collides with a platform-managed resource."""
    norm = normalize_relative_path(path)
    top = norm.split("/", 1)[0]
    if top in RESERVED_TOP_LEVEL:
        return True
    for prefix in RESERVED_RELATIVE_PREFIXES:
        if norm == prefix.rstrip("/") or norm.startswith(prefix):
            return True
    return False


def assert_safe_path(path: str) -> str:
    """Validate a user path: no traversal, no absolute, not reserved. Returns normalised."""
    norm = normalize_relative_path(path)
    if is_reserved(norm):
        raise ValueError(f"path '{path}' collides with a platform-managed resource")
    return norm


def split_path(path: str) -> List[str]:
    return normalize_relative_path(path).split("/")
