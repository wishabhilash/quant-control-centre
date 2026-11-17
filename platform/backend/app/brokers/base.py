"""Broker adapter contract + the helpers every adapter shares.

``BrokerService`` is broker-agnostic *orchestration* (enable / retry /
disable / the scheduled refresh / the connection attempt thread / the
strategy's runtime-credentials section). Everything protocol-specific —
URLs, request shapes, retry rules, wire formats — lives in a per-broker
adapter module here, and adding a broker means adding one adapter module and
registering it in ``app/brokers/__init__.py``. Nothing else changes.

Secrets hygiene (applies to every adapter): no PIN, TOTP key, app secret or
access token is ever logged; the client id appears only masked; request
errors are logged with their *type* only — an error body can contain
credentials.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.broker import BrokerToken

log = get_logger("services.broker")


class BrokerError(Exception):
    """User-facing broker failure (raised by adapters and BrokerService)."""


def _sleep(seconds: float) -> None:
    """Indirection so tests can fast-forward the TOTP-cycle waits."""
    import time

    time.sleep(seconds)


def _seconds_to_next_totp_cycle() -> float:
    """How long until the current 30-second TOTP window rolls over."""
    import time

    return 30 - (int(time.time()) % 30)


def _mask(client_id: str) -> str:
    return (client_id[:4] + "…") if len(client_id) > 4 else "…"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class BrokerAdapter:
    """What a broker must implement for BrokerService to orchestrate it.

    Each adapter is a stateless singleton in ``ADAPTERS`` (registry); settings
    travel with the call so tests can vary ``broker_connect_attempts``.
    """

    # The registry key ("dhan", "fyers", …) — matches BrokerName in models.
    name: str = ""
    # The enable-form request DTO and the SSM credential payload DTO — both
    # live in app/models/broker.py (all DTOs in one place, mirrored by the
    # frontend types).
    enable_model: type = object
    secret_model: type = object

    def normalize(self, req: Any) -> dict:
        """Validate + trim the enable request into the SSM credential payload.

        Raises ``BrokerError`` listing the missing fields — nothing is stored
        until this succeeds (a typo must not leave credentials in SSM).
        """
        raise NotImplementedError

    def totp_key(self, creds: dict) -> str:
        """The TOTP secret inside a stored credential payload (validated
        before anything is stored)."""
        raise NotImplementedError

    def generate_token(self, creds: dict, settings: Settings) -> BrokerToken:
        """Run the broker's token protocol — with its own retry rules — and
        return the access-token payload stored in SSM."""
        raise NotImplementedError
