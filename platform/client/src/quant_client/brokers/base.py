"""The normalized trade interface every broker client implements.

See the module docstring in ``quant_client.brokers`` for the vocabulary and
the design invariants (transport-thin, loud failures, explicit credentials).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --- normalized vocabulary ----------------------------------------------------
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

ORDER_TYPE_MARKET = "MARKET"
ORDER_TYPE_LIMIT = "LIMIT"

PRODUCT_CNC = "CNC"
PRODUCT_INTRADAY = "INTRADAY"
PRODUCT_MARGIN = "MARGIN"

VALIDITY_DAY = "DAY"


class BrokerError(Exception):
    """A broker trade failure carrying the broker's REAL verdict — the
    strategy (or the copy agent) must be able to see the actual error code
    and message, never a swallowed placeholder."""


@dataclass
class OrderRequest:
    """A normalized order intent, broker-agnostic.

    ``instrument_id`` is the broker's NATIVE instrument id (Dhan's
    ``securityId``) — brokers execute by their own ids, so the caller
    supplies it alongside the platform-notation ``symbol``."""

    symbol: str                    # platform notation: EXCHANGE:SYMBOL-SERIES
    instrument_id: str             # broker-native id (e.g. Dhan securityId)
    exchange_segment: str          # broker-native segment (e.g. NSE_FNO)
    side: str                      # SIDE_BUY | SIDE_SELL
    quantity: int
    order_type: str = ORDER_TYPE_MARKET
    price: float = 0.0             # LIMIT price (ignored for MARKET)
    product: str = PRODUCT_MARGIN
    validity: str = VALIDITY_DAY

    def __post_init__(self) -> None:
        if self.side not in (SIDE_BUY, SIDE_SELL):
            raise BrokerError(f"invalid side: {self.side!r}")
        if self.order_type not in (ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT):
            raise BrokerError(f"invalid order_type: {self.order_type!r}")
        if self.order_type == ORDER_TYPE_LIMIT and self.price <= 0:
            raise BrokerError("LIMIT orders need a positive price")
        if int(self.quantity) <= 0:
            raise BrokerError(f"invalid quantity: {self.quantity!r}")


@dataclass
class OrderResult:
    """The normalized result of one order placement.

    ``status`` is the BROKER's own order status (e.g. TRADED / TRANSIT /
    PENDING / PART_TRADED / REJECTED) — the caller decides what counts as
    filled. ``traded_price`` is 0.0 until the broker reports a fill."""

    order_id: str
    status: str
    traded_price: float = 0.0
    filled_qty: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


class BrokerClient(ABC):
    """The normalized trade surface. Implementations translate to one
    broker's wire protocol; they must stay transport-thin (no execution
    policy) and loud (real errors, real statuses)."""

    #: short name for logs/journals ("dhan", …)
    name: str = "?"

    @abstractmethod
    def place_order(self, req: OrderRequest) -> OrderResult:
        """Place one order; returns its id + the broker's immediate status."""

    @abstractmethod
    def modify_order_to_market(self, order_id: str, quantity: int) -> OrderResult:
        """Convert a working order's remaining quantity to MARKET (the
        standard fallback path for an unfilled LIMIT)."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a working order; True when the broker accepted it."""

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """One order's raw record from the broker's order book (None when
        not found). Implementations must surface the broker's own field
        names via ``raw`` and the normalized fill price via
        :meth:`fill_price_of`."""

    @abstractmethod
    def available_balance(self) -> float:
        """The account's available cash — loud on failure (never a silent
        zero: a funds call that fails must abort the trade, not pretend the
        account is empty)."""

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        """The account's open positions (raw broker records) — the copy
        agent's drift checks read from here."""

    # --- shared helpers ------------------------------------------------------
    @staticmethod
    def fill_price_of(order: Dict[str, Any]) -> float:
        """The fill price from a broker order-book record, tolerant of each
        broker's field name (Dhan: ``averageTradedPrice``; the trade-book
        endpoints use ``tradedPrice``). 0.0 when nothing has traded."""
        for key in ("averageTradedPrice", "tradedPrice"):
            value = order.get(key)
            if value:
                return float(value)
        return 0.0

    @staticmethod
    def filled_qty_of(order: Dict[str, Any]) -> int:
        return int(order.get("filledQty", 0) or 0)
