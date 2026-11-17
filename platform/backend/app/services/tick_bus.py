"""The tick bus: a transient Redis pub/sub the per-account feeds publish to.

Golden rule 1 says S3 is the single source of truth and forbids a second data
store. A pub/sub channel is deliberately NOT a store: nothing is persisted,
there is no history and no replay — a message exists only while it is in
flight to subscribers. The feed stamps every tick (exchange + gateway
timestamps) and consumers live with gaps; the *desired subscription set* is
the only durable state and it lives in the broker document (S3).

Channels: ``ticks:{user_id}:{broker}`` — one per user+broker account, so
account data is never fanned out across accounts (broker ToS / data
redistribution). Everything is localhost (REDIS_URL); the bus is fire-and-
forget by construction: ``publish`` never raises on a Redis hiccup, a tick
lost in transit is simply a gap.

A sibling ``ticks:{user_id}:{broker}:control`` channel carries *requests*
from strategy processes back to the feed worker (e.g. "subscribe these
symbols at runtime") — the strategy-side ``quant_client`` lib publishes
there; the worker answers with an ``ack`` frame on the data channel.
"""
from __future__ import annotations

import json
from typing import Iterator, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger("services.tickbus")

CHANNEL_PREFIX = "ticks"


def channel_for(user_id: str, broker: str) -> str:
    """The pub/sub channel for one user+broker account."""
    return f"{CHANNEL_PREFIX}:{user_id}:{broker}"


def control_channel_for(user_id: str, broker: str) -> str:
    """The control channel for one user+broker account: strategy-side
    ``quant_client`` processes publish *requests* here (subscribe …);
    the feed worker answers on the data channel."""
    return f"{channel_for(user_id, broker)}:control"


class TickBus:
    """Thin wrapper over Redis pub/sub (lazy client; publishes never raise)."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._client = None

    def _redis(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self.settings.redis_url, decode_responses=True,
                socket_connect_timeout=3, socket_timeout=3)
        return self._client

    def pubsub(self, channel: str):
        """A live pubsub subscribed to one channel — for long-lived listeners
        (the feed worker's control thread) that poll ``get_message`` with
        their own timeout. Raises on a Redis failure; the caller decides how
        to survive it (the control thread retries; publish paths never see
        this)."""
        pubsub = self._redis().pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(channel)
        return pubsub

    def publish(self, channel: str, payload: dict) -> bool:
        """Publish one JSON message; returns False (never raises) when Redis
        is unreachable — a dropped tick is a gap, not an error."""
        try:
            return bool(self._redis().publish(channel, json.dumps(payload)))
        except Exception as exc:  # noqa: BLE001 — the feed must survive Redis
            log.warning("tick publish failed on %s: %s", channel,
                        type(exc).__name__)
            return False

    def messages(self, channel: str):
        """Yield decoded messages from one channel (blocking per message,
        with a ~1 s poll so callers can interleave their own timeouts)."""
        pubsub = self._redis().pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(channel)
        try:
            while True:
                msg = pubsub.get_message(timeout=1.0)
                if msg is None or msg.get("type") != "message":
                    continue
                try:
                    yield json.loads(msg["data"])
                except (ValueError, TypeError):
                    continue
        finally:
            try:
                pubsub.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
