"""The broker adapter registry.

``SUPPORTED_BROKERS`` and the enable-request models are derived from here —
adding a broker is: a new adapter module (subclass ``BrokerAdapter``, keep its
protocol details, URLs and retry rules self-contained), one entry in
``ADAPTERS`` below, one literal in ``BrokerName`` (``app/models/broker.py``,
guarded by a test), and the frontend's enable form. ``BrokerService`` needs
no change.
"""
from __future__ import annotations

from app.brokers.base import BrokerAdapter, BrokerError
from app.brokers.dhan import DhanAdapter
from app.brokers.fyers import FyersAdapter

# Every broker the platform knows about, in UI order.
ADAPTERS: dict[str, BrokerAdapter] = {
    a.name: a for a in (DhanAdapter(), FyersAdapter())
}

SUPPORTED_BROKERS: tuple[str, ...] = tuple(ADAPTERS)


def get_adapter(broker: str) -> BrokerAdapter:
    """The adapter for ``broker`` — ``BrokerError`` for an unknown name."""
    adapter = ADAPTERS.get(broker)
    if adapter is None:
        raise BrokerError(f"unsupported broker: {broker}")
    return adapter
