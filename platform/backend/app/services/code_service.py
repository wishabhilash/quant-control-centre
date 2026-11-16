"""Project code service.

Owns the base64 encoding of project files, safe ZIP import and project
synchronisation. The platform is agnostic about filenames, file types and
directory hierarchy (spec sections 7, 8).
"""
from __future__ import annotations

import io
import os
import stat
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.paths import assert_safe_path, is_reserved, normalize_relative_path
from app.models.strategy import (
    CodeBlock,
    CodeUpdate,
    DEFAULT_ENTRYPOINT,
    RuntimeFileContent,
    RuntimeFileInfo,
)
from app.storage.strategy_store import StrategyStore

log = get_logger("services.code")


class CodeError(Exception):
    pass


# Runtime files live in the strategy directory (written by the strategy process
# at run time). The editor lists them read-only, never the platform's own or
# hidden entries: log files, the pid file, and anything dot-hidden.
RUNTIME_EXCLUDED_NAMES = {"strategy.log", "runner.log", "strategy.pid"}
RUNTIME_MAX_FILES = 5000          # listing cap (mirrors max_upload_files spirit)
RUNTIME_MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MB per content read


def _runtime_excluded(name: str) -> bool:
    """A log file, the pid file, or dot-hidden → never shown."""
    return name.startswith(".") or name in RUNTIME_EXCLUDED_NAMES or name.endswith(".log")


def _ext_to_language(path: str) -> str:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return {
        "py": "python", "js": "javascript", "ts": "typescript", "tsx": "typescript",
        "json": "json", "csv": "csv", "txt": "plaintext", "md": "markdown",
        "yaml": "yaml", "yml": "yaml", "sh": "shell", "ini": "ini", "toml": "ini",
    }.get(ext, "plaintext")


def language_for(path: str) -> str:
    """Map a relative path to a Monaco language id for syntax highlighting."""
    return _ext_to_language(path)


class CodeService:
    def __init__(self, store: StrategyStore | None = None, settings: Settings | None = None):
        self.store = store or StrategyStore()
        self.settings = settings or get_settings()

    # --- encoding ---------------------------------------------------------
    @staticmethod
    def encode_project(files: Dict[str, str]) -> CodeBlock:
        """Validate paths and produce a base64-encoded CodeBlock.

        The entry point is fixed to ``main.py`` (DEFAULT_ENTRYPOINT) and is not
        configurable; it must be present and be a Python file (golden rule 2).
        """
        encoded: Dict[str, str] = {}
        for rel_path, text in files.items():
            try:
                norm = assert_safe_path(rel_path)
            except ValueError as exc:
                raise CodeError(str(exc)) from exc
            if norm in encoded:
                # duplicate after normalisation
                raise CodeError(f"duplicate file path after normalisation: {norm}")
            encoded[norm] = StrategyStore.encode_file(text)
        # The entry point is fixed: it must exist and be a Python file.
        if DEFAULT_ENTRYPOINT not in encoded:
            raise CodeError(f"entry point {DEFAULT_ENTRYPOINT!r} is required but missing from the project")
        if not DEFAULT_ENTRYPOINT.endswith(".py"):
            raise CodeError("entry point must be a .py file")
        return CodeBlock(encoding="base64", files=encoded)

    @staticmethod
    def decode_project(code: CodeBlock) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for rel_path, b64 in code.files.items():
            try:
                norm = assert_safe_path(rel_path)
            except ValueError as exc:
                raise CodeError(str(exc)) from exc
            out[norm] = StrategyStore.decode_file(b64)
        return out

    # --- project save -----------------------------------------------------
    def save_code(self, strategy_id: str, update: CodeUpdate) -> CodeBlock:
        doc = self.store.get_document(strategy_id)
        encoded = self.encode_project(update.files)
        doc.code = encoded
        try:
            self.store.update_document(doc, if_match_etag=update.if_match_etag)
        except Exception:
            raise
        return doc.code

    # --- ZIP import -------------------------------------------------------
    def import_zip(self, strategy_id: str, zip_bytes: bytes) -> Tuple[CodeBlock, List[str]]:
        """Import an uploaded project ZIP.

        Returns (CodeBlock, list_of_imported_paths). Enforces path-safety and
        size/count limits; rejects traversal, absolute paths, symlinks and
        collisions with platform-managed resources (spec section 7).
        """
        s = self.settings
        if len(zip_bytes) > s.max_upload_bytes:
            raise CodeError(f"ZIP exceeds max upload size ({s.max_upload_bytes} bytes)")
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as exc:
            raise CodeError("invalid ZIP archive") from exc

        with zf:
            infos = zf.infolist()
            file_entries = [i for i in infos if not i.is_dir()]
            if len(file_entries) > s.max_upload_files:
                raise CodeError(f"ZIP exceeds max file count ({s.max_upload_files})")
            imported: Dict[str, str] = {}
            for info in file_entries:
                # The unix mode (incl. the 12 type bits) lives in the high 16
                # bits of external_attr. Use the full unix mode (0o777777):
                # S_ISREG/S_ISLNK need the 12 type bits, and Python's
                # zipfile.writestr omits S_IFREG (type bits = 0), which must
                # still be treated as a regular file.
                mode = (info.external_attr >> 16) & 0o777777
                if stat.S_ISLNK(mode):
                    raise CodeError(f"symlinks are not allowed: {info.filename}")
                # Reject only *explicitly* declared non-regular types
                # (fifo/socket/device); a zero/unknown type is a regular file.
                if (mode & 0o170000) and not stat.S_ISREG(mode):
                    raise CodeError(f"non-regular file not allowed: {info.filename}")
                # Reject absolute / traversal / reserved.
                raw = info.filename.replace("\\", "/")
                if os.path.isabs(raw) or raw.startswith("/") or ".." in raw.split("/"):
                    raise CodeError(f"unsafe path in ZIP: {raw}")
                norm = assert_safe_path(raw)
                if is_reserved(norm):
                    raise CodeError(f"ZIP path collides with reserved resource: {norm}")
                data = zf.read(info)
                if len(data) > s.max_single_file_bytes:
                    raise CodeError(f"file exceeds max single-file size: {norm}")
                try:
                    imported[norm] = data.decode("utf-8")
                except UnicodeDecodeError:
                    # Binary asset: store as base64 via the store directly.
                    imported[norm] = "\x00BINARY\x00" + __import__("base64").b64encode(data).decode("ascii")
            if not imported:
                raise CodeError("ZIP contains no importable files")

            # The entry point is fixed to main.py and must be present in the ZIP.
            if DEFAULT_ENTRYPOINT not in imported:
                raise CodeError(f"ZIP must contain an entry point {DEFAULT_ENTRYPOINT!r}")

            code = self.encode_project(imported)
        # Persist into the strategy document.
        doc = self.store.get_document(strategy_id)
        doc.code = code
        self.store.update_document(doc)
        log.info("imported ZIP into %s: %d files", strategy_id, len(code.files))
        return code, sorted(code.files.keys())

    def export_zip_bytes(self, strategy_id: str) -> bytes:
        doc = self.store.get_document(strategy_id)
        files = self.decode_project(doc.code)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_path, text in files.items():
                zf.writestr(rel_path, text)
        return buf.getvalue()

    # --- runtime files ------------------------------------------------------
    # Files the strategy itself wrote into its directory at run time (state,
    # downloaded data, ...). They are NOT part of the S3 project (golden rule 1:
    # S3 stays the source of truth for project source), so the editor shows
    # them in a separate read-only section — visible, never silently promoted
    # into the project, and deploy-sync semantics stay untouched.
    def strategy_dir(self, strategy_id: str) -> Path:
        return self.settings.strategies_dir_path() / strategy_id

    def list_runtime_files(self, strategy_id: str) -> List[RuntimeFileInfo]:
        """Runtime files on disk, excluding logs, the pid file and dot-hidden
        entries (at any depth). Empty when the directory does not exist yet."""
        import datetime

        root = self.strategy_dir(strategy_id)
        if not root.is_dir():
            return []
        found: List[RuntimeFileInfo] = []
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune hidden directories entirely (.venv, .git, ...) and keep the
            # walk order deterministic.
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if _runtime_excluded(name):
                    continue
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                except OSError:  # raced with a delete — skip it
                    continue
                found.append(RuntimeFileInfo(
                    path=p.relative_to(root).as_posix(),
                    size_bytes=st.st_size,
                    modified_at=datetime.datetime.fromtimestamp(
                        st.st_mtime, datetime.timezone.utc
                    ).replace(microsecond=0).isoformat(),
                ))
                if len(found) >= RUNTIME_MAX_FILES:
                    break
            if len(found) >= RUNTIME_MAX_FILES:
                break
        found.sort(key=lambda f: f.path)
        return found

    def read_runtime_file(self, strategy_id: str, rel_path: str) -> RuntimeFileContent:
        """Content of one runtime file (text, or base64 for binary), capped at
        RUNTIME_MAX_READ_BYTES with a ``truncated`` flag.

        The same exclusion policy as the listing applies — logs, the pid file,
        dot-hidden entries and any dot-hidden path component are refused, as is
        anything that escapes the strategy directory (traversal, absolute
        paths, symlinks pointing outside).
        """
        import base64

        norm = normalize_relative_path(rel_path)  # ValueError on traversal/absolute
        if any(part.startswith(".") for part in norm.split("/")):
            raise ValueError("hidden files are not readable through the editor")
        if is_reserved(norm):
            raise ValueError(f"path '{norm}' is a platform-managed resource")
        if any(part.endswith(".log") for part in norm.split("/")):
            raise ValueError(f"log files are not readable through the editor: {norm}")

        root = self.strategy_dir(strategy_id)
        p = root / norm
        # A runtime file could be a symlink planted by the strategy; refuse
        # anything that resolves outside the strategy directory.
        try:
            resolved = p.resolve()
            resolved.relative_to(root.resolve())
        except (ValueError, OSError) as exc:
            raise ValueError("path escapes the strategy directory") from exc
        if not resolved.is_file():
            raise FileNotFoundError(f"no runtime file '{norm}'")

        data = resolved.read_bytes()[: RUNTIME_MAX_READ_BYTES + 1]
        truncated = len(data) > RUNTIME_MAX_READ_BYTES
        data = data[: RUNTIME_MAX_READ_BYTES]
        try:
            content = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(data).decode("ascii")
            encoding = "base64"
        return RuntimeFileContent(
            strategy_id=strategy_id, path=norm, encoding=encoding,
            truncated=truncated, content=content,
        )
