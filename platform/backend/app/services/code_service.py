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
from typing import Dict, List, Tuple

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.paths import assert_safe_path, is_reserved
from app.models.strategy import CodeBlock, CodeUpdate
from app.storage.strategy_store import StrategyStore

log = get_logger("services.code")


class CodeError(Exception):
    pass


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
    def encode_project(files: Dict[str, str], entrypoint: str) -> CodeBlock:
        """Validate paths and produce a base64-encoded CodeBlock."""
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
        # entrypoint must exist and be python
        if entrypoint not in encoded:
            raise CodeError("entrypoint not present in project files")
        if not entrypoint.endswith(".py"):
            raise CodeError("entrypoint must be a .py file")
        return CodeBlock(encoding="base64", entrypoint=entrypoint, files=encoded)

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
        encoded = self.encode_project(update.files, update.entrypoint)
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

            # Determine entry point: prefer a declared main.py else first .py by path.
            entrypoint = None
            if "main.py" in imported:
                entrypoint = "main.py"
            else:
                py_files = sorted(p for p in imported if p.endswith(".py"))
                if py_files:
                    entrypoint = py_files[0]
            if entrypoint is None:
                raise CodeError("ZIP does not contain any Python entry point")

            code = self.encode_project(imported, entrypoint)
        # Persist into the strategy document.
        doc = self.store.get_document(strategy_id)
        doc.code = code
        self.store.update_document(doc)
        log.info("imported ZIP into %s: %d files, entrypoint=%s", strategy_id, len(code.files), entrypoint)
        return code, sorted(code.files.keys())

    def export_zip_bytes(self, strategy_id: str) -> bytes:
        doc = self.store.get_document(strategy_id)
        files = self.decode_project(doc.code)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_path, text in files.items():
                zf.writestr(rel_path, text)
        return buf.getvalue()
