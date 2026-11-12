"""Filesystem path helpers and reserved-path protection.

The platform must never let a Monaco project overwrite or delete
platform-managed resources (``.venv/`` and ``strategy.log``). These helpers
centralise that policy and are used by the code, deployment and ZIP-import
services.

They also enforce **directory ownership**: the platform only ever writes into
a strategies root (and strategy directories) that it created itself. A
directory that already exists without the ownership marker is treated as
foreign content (operator data, a previous install's leftovers) and is
refused — a misconfigured ``STRATEGIES_DIR`` can never silently overwrite
existing strategies or unrelated data.
"""
from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from typing import List, Sequence

# Ownership marker written into the strategies root and every strategy
# directory the platform creates. Presence = "created/managed by this platform".
MANAGED_MARKER = ".quant-managed"

# Directory entries that do not make a directory "foreign" (VCS/OS artefacts a
# fresh checkout may contain); anything else without the marker is refused.
_ADOPTABLE_ARTEFACTS = {".gitkeep", ".DS_Store"}

# Platform-managed entries reserved inside every strategy project directory.
# Includes the ownership marker: user projects can neither overwrite it
# (forgery) nor have it deleted by a deploy sync.
RESERVED_TOP_LEVEL = {".venv", "strategy.log", "strategy.pid", MANAGED_MARKER}

# Runtime-managed relative paths that must never appear in a user project.
RESERVED_RELATIVE_PREFIXES: Sequence[str] = (".venv/", "strategy.log", "strategy.pid")


class UnownedDirectoryError(RuntimeError):
    """The target directory exists but was not created by this platform."""


def is_managed(path: Path) -> bool:
    """True if ``path`` carries the platform ownership marker."""
    return (path / MANAGED_MARKER).exists()


def ensure_managed_dir(path: Path) -> Path:
    """Create ``path`` if needed and mark it as platform-owned.

    Refuses (``UnownedDirectoryError``) when the directory already exists with
    content but no marker — the platform must never adopt or overwrite foreign
    content. Empty directories are adopted and marked (safe: nothing to lose);
    a fresh checkout may contain VCS artefacts only (``.gitkeep``, ``.DS_Store``)
    and is still considered adoptable.
    """
    path.mkdir(parents=True, exist_ok=True)
    if not is_managed(path):
        if any(p.name not in _ADOPTABLE_ARTEFACTS for p in path.iterdir()):
            raise UnownedDirectoryError(
                f"refusing to manage '{path}': directory exists without the "
                f"'{MANAGED_MARKER}' ownership marker (set STRATEGIES_DIR to a "
                "dedicated location, or move the existing content away)"
            )
        (path / MANAGED_MARKER).write_text(
            "Created by the Quant Strategy Control Centre. Do not edit.\n",
            encoding="utf-8",
        )
    return path


_STRATEGY_DIR_PATTERN = re.compile(r"^STR-\d{3}$")


def ensure_managed_strategies_root(root: Path) -> Path:
    """Ownership guard for the strategies ROOT (fail fast at service startup).

    The root is adopted when it is empty, carries only VCS/OS artefacts, or
    contains only directories named ``STR-NNN`` (strategy dirs created by a
    platform version predating the marker — they are still guarded per-directory
    at every destructive moment: deploy sync and delete). Anything else means
    this is foreign data and the platform refuses to manage it.
    """
    root.mkdir(parents=True, exist_ok=True)
    if not is_managed(root):
        foreign = [
            p.name
            for p in root.iterdir()
            if p.name not in _ADOPTABLE_ARTEFACTS
            and not (p.is_dir() and _STRATEGY_DIR_PATTERN.match(p.name))
        ]
        if foreign:
            raise UnownedDirectoryError(
                f"refusing to manage strategies root '{root}': found non-strategy "
                f"content {foreign} without the '{MANAGED_MARKER}' ownership "
                "marker (set STRATEGIES_DIR to a dedicated location, or move "
                "the existing content away)"
            )
        (root / MANAGED_MARKER).write_text(
            "Created by the Quant Strategy Control Centre. Do not edit.\n",
            encoding="utf-8",
        )
    return root


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
