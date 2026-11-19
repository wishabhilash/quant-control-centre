"""The common broker-trade interface.

ONE normalized surface for every trade function, with per-broker
implementations behind it — so the copy-trading gateway (leader side) and the
mirroring agent (follower side) speak the same language regardless of which
broker protocol is underneath.

Design invariants:

- **Transport-thin**: implementations map a NORMALIZED request to the
  broker's wire format and back; no execution policy lives here (polling,
  market-fallback, retries are the gateway's/agent's concern, composed ON
  TOP of these clients).
- **Loud failures**: broker errors surface as :class:`BrokerError` carrying
  the broker's REAL verdict (error code / type / message) — never swallowed
  into a silent zero or a fake success.
- **Explicit credentials**: callers supply them (a strategy from its runtime
  config; the copy agent from the platform's credential store) — this module
  never reads secrets itself.

Normalized vocabulary (broker-neutral):

- ``side``: ``BUY`` | ``SELL``
- ``order_type``: ``MARKET`` | ``LIMIT``
- ``product``: ``CNC`` | ``INTRADAY`` | ``MARGIN``
- ``symbol``: platform notation ``EXCHANGE:SYMBOL-SERIES`` (e.g. NSE:SBIN-EQ)
- ``instrument_id``: the broker's NATIVE instrument id (e.g. Dhan's
  ``securityId``) — brokers trade by their own ids, so the caller supplies
  both.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .base import (BrokerClient, BrokerError, OrderRequest, OrderResult,
                   ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET, PRODUCT_CNC,
                   PRODUCT_INTRADAY, PRODUCT_MARGIN, SIDE_BUY, SIDE_SELL)
from .dhan import DhanClient

BROKERS = {"dhan": DhanClient}

__all__ = [
    "BrokerClient", "BrokerError", "OrderRequest", "OrderResult",
    "DhanClient", "BROKERS",
    "SIDE_BUY", "SIDE_SELL", "ORDER_TYPE_MARKET", "ORDER_TYPE_LIMIT",
    "PRODUCT_CNC", "PRODUCT_INTRADAY", "PRODUCT_MARGIN",
]
