"""Stdlib stand-in for ``fyers_apiv3.fyers_logger`` (which imports
``aws_lambda_powertools`` — one of the SDK deps this vendor deliberately
avoids). Same call surface, platform logging instead of a Lambda logger.

Deliberately **no file handler**: the upstream SDK would write a
``fyersDataSocket.log`` into the current working directory (a stray log file
outside the platform's logging design); everything goes to the platform's
logger instead.
"""
from __future__ import annotations

import logging


class FyersLogger:
    """Minimal, call-compatible replacement for the SDK's logger wrapper."""

    def __init__(self, name: str, level: str = "DEBUG", stack_level: int = 1,
                 logger_handler: logging.Handler | None = None):
        self._log = logging.getLogger(f"brokers.vendor.{name}")
        self._log.setLevel(getattr(logging, level, logging.DEBUG))
        if logger_handler is not None:  # pragma: no cover — never passed here
            self._log.addHandler(logger_handler)

    def debug(self, message: str, *args: object) -> None:
        self._log.debug(message, *args)

    def info(self, message: str, *args: object) -> None:
        self._log.info(message, *args)

    def warning(self, message: str, *args: object) -> None:
        self._log.warning(message, *args)

    def error(self, message: str, *args: object) -> None:
        self._log.error(message, *args)

    def exception(self, message: str, *args: object) -> None:
        self._log.exception(message, *args)
