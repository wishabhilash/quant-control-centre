"""quant_client — the strategy-side client for the platform's tick gateway.

Installed into every strategy venv by the deploy (option 1: the wheel ships
inside the deploy artifact; never on PyPI). See README.md for the shape.
"""
from .feed import TickFeed, TickFeedError, TickStaleError, load_runtime_config

__all__ = ["TickFeed", "TickFeedError", "TickStaleError", "load_runtime_config"]
__version__ = "0.4.0"
