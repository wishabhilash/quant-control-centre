"""Log service.

Exactly one log file per strategy (``strategy.log``). Implements a bounded
handler that truncates old content when the size limit is reached — it does NOT
create rotating backup files. Provides historical retrieval, live tailing,
level filtering and download (spec section 13).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import AsyncIterator, List, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.process_service import ProcessService

log = get_logger("services.logs")

_LEVEL_RE = re.compile(r"\|\s*(INFO|WARNING|ERROR|DEBUG|CRITICAL)\s*\|", re.IGNORECASE)
_LEVELS = {"INFO", "WARNING", "ERROR", "DEBUG", "CRITICAL"}


def parse_level(line: str) -> str:
    m = _LEVEL_RE.search(line)
    return m.group(1).upper() if m else "INFO"


class BoundedLogHandler:
    """Writes to a single physical file, truncating the head when full."""

    def __init__(self, path: Path, max_bytes: int):
        self.path = Path(path)
        self.max_bytes = max_bytes

    def ensure_bounded(self) -> None:
        if not self.path.exists():
            return
        size = self.path.stat().st_size
        if size <= self.max_bytes:
            return
        # Keep only the most recent tail within the limit.
        keep = self.max_bytes
        with open(self.path, "rb") as fh:
            fh.seek(max(0, size - keep))
            tail = fh.read()
        # Truncate to the last newline boundary so we don't cut a line.
        nl = tail.find(b"\n")
        if nl != -1:
            tail = tail[nl + 1:]
        with open(self.path, "wb") as fh:
            fh.write(tail)
        log.info("bounded log %s to %d bytes", self.path, len(tail))

    def write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(text)
        self.ensure_bounded()


class LogService:
    def __init__(self, settings: Settings | None = None, process_service: Optional[ProcessService] = None):
        self.settings = settings or get_settings()
        self.process_service = process_service or ProcessService(self.settings)

    def log_path(self, strategy_id: str) -> Path:
        return self.process_service.log_path(strategy_id)

    def handler(self, strategy_id: str) -> BoundedLogHandler:
        return BoundedLogHandler(self.log_path(strategy_id), self.settings.strategy_log_max_bytes)

    # --- historical retrieval --------------------------------------------
    def get_lines(
        self,
        strategy_id: str,
        tail: Optional[int] = None,
        since: Optional[int] = None,
        level: Optional[str] = None,
        search: Optional[str] = None,
    ) -> dict:
        path = self.log_path(strategy_id)
        if not path.exists():
            return {"strategy_id": strategy_id, "lines": [], "offset": 0, "next_offset": 0,
                    "total_lines": 0, "size_bytes": 0}
        raw = path.read_text(encoding="utf-8", errors="replace")
        all_lines = raw.splitlines()
        # since: number of lines to skip from the start (for incremental fetch).
        start = since or 0
        selected = all_lines[start:]
        if level and level.upper() in _LEVELS:
            selected = [ln for ln in selected if parse_level(ln) == level.upper()]
        if search:
            needle = search.lower()
            selected = [ln for ln in selected if needle in ln.lower()]
        if tail is not None:
            selected = selected[-tail:]
        next_offset = start + len(selected)
        return {
            "strategy_id": strategy_id,
            "lines": selected,
            "offset": start,
            "next_offset": next_offset,
            "total_lines": len(all_lines),
            "size_bytes": path.stat().st_size,
        }

    def download(self, strategy_id: str) -> bytes:
        path = self.log_path(strategy_id)
        if not path.exists():
            return b""
        return path.read_bytes()

    # --- live streaming ---------------------------------------------------
    async def stream(self, strategy_id: str, follow: bool = True,
                     interval: float = 1.0) -> AsyncIterator[dict]:
        """Yield new log lines as they appear. Used by the WebSocket endpoint."""
        import asyncio

        path = self.log_path(strategy_id)
        offset = 0
        if path.exists():
            # Start from the current end (don't replay history on connect).
            offset = path.stat().st_size
        while True:
            if path.exists():
                size = path.stat().st_size
                if size > offset:
                    with open(path, "rb") as fh:
                        fh.seek(offset)
                        chunk = fh.read()
                    offset = size
                    for line in chunk.decode("utf-8", errors="replace").splitlines():
                        yield {"type": "line", "line": line, "level": parse_level(line)}
                elif size < offset:
                    # file was truncated/bounded
                    offset = size
            if not follow:
                break
            await asyncio.sleep(interval)
