"""Platform logging configuration.

The platform itself logs to the standard handlers. Each *strategy* logs to its
own single bounded log file (see services/log_service.py); this module only
configures the backend's own logging.
"""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup_platform_logging(level: str = "INFO") -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("quant_platform")
    if _CONFIGURED:
        return logger
    logger.setLevel(level.upper())
    logger.propagate = False
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    _CONFIGURED = True
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"quant_platform.{name}")
