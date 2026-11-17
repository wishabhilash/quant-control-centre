"""The strategy-side tick feed.

A strategy talks to its account's market-data gateway through this one class —
never to a broker socket, and never to the platform's API. It is installed into
every strategy venv by the deploy (it ships inside the deploy artifact, not on
PyPI — see README.md), so it shares the feed worker's wire protocol:

    strategy ──publish──▶ ticks:{user}:{broker}:control   (a request)
    feed worker ──ack──▶ ticks:{user}:{broker}            (one frame)
    feed worker ──ticks──▶ ticks:{user}:{broker}          (the stream)

Two guarantees the platform makes, enforced here once instead of per strategy:

- **``ensure(symbols)``** is *add-only* and idempotent. It asks the feed worker
  to subscribe the symbols (persisted in S3 by the worker) and waits for an
  ``ack`` frame, which carries the last tick per symbol — a strategy joining
  mid-session gets current state immediately. It *raises* on failure; it never
  returns as if ticks were flowing when they were not.
- **``stream(max_age=N)``** is a blocking, staleness-enforced iterator. A tick
  whose own timestamp is older than ``N`` seconds, or a liveness gap beyond the
  heartbeat cadence, raises :class:`TickStaleError` — the "never trade on a
  dead feed" rule, enforced in the client rather than in every strategy's
  discipline.

Reconnect with a small backoff on Redis blips lives here too. Redis is a wire,
not a store: a strategy joining late misses nothing it was not already
subscribed to, and the snapshot from ``ensure`` is how it catches up.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, Iterable, Iterator, Optional


class TickFeedError(RuntimeError):
    """A tick-feed contract error (no gateway, a rejected subscription, a
    missing ack). A strategy should treat this as "data is unavailable" and
    refuse to place orders, not as a transient glitch to retry blindly."""


class TickStaleError(TickFeedError):
    """Ticks are older than the staleness window, or the feed has gone silent
    past the heartbeat cadence. Raising — not yielding stale data — is the
    whole point: trading on a dead feed is the failure this guards against."""


#: How long to wait for the feed worker's ack of a subscription request.
DEFAULT_ACK_TIMEOUT = 10.0
#: Redis reconnect backoff (seconds) — a short, capped wait.
_RECONNECT_BACKOFF = 1.0
_RECONNECT_MAX_BACKOFF = 10.0


def load_runtime_config() -> Dict[str, Any]:
    """Read the platform's runtime config the runner drops in at launch.

    Mirrors the starter template's ``load_config()``: the same
    ``QUANT_RUNTIME_CONFIG`` file, so a strategy that prefers the raw runtime
    config (over the parameter-merged view) can build a :class:`TickFeed` from
    it. Returns ``{}`` when the file is absent or unparseable — a strategy
    without a feed simply has no gateway, and :meth:`TickFeed._resolve` says so.
    """
    path = os.environ.get("QUANT_RUNTIME_CONFIG")
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class TickFeed:
    """One account's tick stream, discovered from the runtime config.

    Usage::

        from quant_client import TickFeed
        feed = TickFeed(config)              # config = load_config()
        feed.ensure(["NSE:SBIN-EQ"])        # add-only, idempotent
        for tick in feed.stream(max_age=10):
            print(tick["instrument"]["symbol"], tick["ltp"])

    ``config`` is the strategy's merged runtime config (the starter's
    ``load_config()``) — it must carry a ``data`` section the runner injected,
    i.e. the account's enabled tick feed(s). A strategy with no feed gets a
    clear error from the constructor, never a silent no-op.
    """

    def __init__(self, config: Dict[str, Any], broker: str = "fyers",
                 ack_timeout: float = DEFAULT_ACK_TIMEOUT,
                 client: Any = None):
        self._gateway = self._resolve(config, broker)
        self.broker = self._gateway["broker"]
        self.channel = self._gateway["channel"]
        self.control_channel = f"{self.channel}:control"
        self._ack_timeout = ack_timeout
        self._client = client  # injected for tests; built lazily otherwise
        self._pubsub: Any = None
        self._reconnect_at = 0.0
        self._last_frame_wall: Optional[float] = None
        self._snapshot: Dict[str, dict] = {}
        self._lock = threading.Lock()
        # Eagerly open the stream: a bad URL or unreachable Redis fails here,
        # at construction, rather than three lines into a strategy.
        self._connect()

    # --- discovery -----------------------------------------------------------
    @staticmethod
    def _resolve(config: Dict[str, Any], broker: str) -> dict:
        """Pick one enabled gateway from the runtime config's ``data`` section.

        With a single gateway (v0: only Fyers) the ``broker`` arg is ignored;
        with several it selects. No enabled feed → a clear error, so a strategy
        never silently trades without data.
        """
        data = (config or {}).get("data") or {}
        gateway = data.get("gateway") or {}
        if not gateway:
            raise TickFeedError(
                "no tick gateway in the runtime config — enable a feed for "
                "your broker on the dashboard (Brokers tab)")
        if broker in gateway:
            info = gateway[broker]
        elif len(gateway) == 1:
            broker, info = next(iter(gateway.items()))
        else:
            raise TickFeedError(
                f"several tick gateways available "
                f"({', '.join(sorted(gateway))}) — pass which one, e.g. "
                f"TickFeed(config, broker='fyers')")
        for key in ("channel", "url"):
            if not info.get(key):
                raise TickFeedError(f"tick gateway {broker!r} is missing "
                                    f"{key!r}")
        info = dict(info)
        info["broker"] = broker
        return info

    # --- the socket ----------------------------------------------------------
    def _connect(self) -> None:
        if self._client is None:
            try:
                import redis
            except ImportError as exc:  # noqa: BLE001 — a clear, actionable msg
                raise TickFeedError(
                    "quant_client needs 'redis' (pip install redis) — the "
                    "deploy installs it; a local strategy must add it to its "
                    "requirements.txt") from exc
            self._client = redis.Redis.from_url(
                self._gateway["url"], decode_responses=True,
                socket_connect_timeout=3, socket_timeout=3)
        self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        self._pubsub.subscribe(self.channel)

    def _reconnect(self) -> None:
        """Re-open the pub/sub after a Redis blip, with a short capped backoff.

        Redis is a wire: a broken socket is recoverable, so we come back rather
        than die — but only when the client is up (a permanently dead Redis is
        surfaced as staleness, not an infinite loop here)."""
        now = time.monotonic()
        if now < self._reconnect_at:
            return
        self._reconnect_at = now + min(_RECONNECT_BACKOFF * 4, _RECONNECT_MAX_BACKOFF)
        try:
            if self._pubsub is not None:
                try:
                    self._pubsub.close()
                except Exception:  # noqa: BLE001 — best-effort
                    pass
            self._connect()
        except Exception:  # noqa: BLE001 — Redis still down; try again soon
            self._pubsub = None

    def _next_frame(self, timeout: float) -> Optional[dict]:
        """Block for one decoded frame on the data channel, or None.

        A Redis socket error triggers a reconnect (best-effort) and yields None
        for this cycle — the caller's staleness logic turns a *sustained* gap
        into a :class:`TickStaleError`."""
        with self._lock:
            pubsub = self._pubsub
        if pubsub is None:
            self._reconnect()
            return None
        try:
            message = pubsub.get_message(timeout=timeout)
        except Exception:  # noqa: BLE001 — a blip, not the end of the stream
            self._reconnect()
            return None
        if not message or message.get("type") != "message":
            return None
        try:
            frame = json.loads(message["data"])
        except (ValueError, TypeError):
            return None
        return frame if isinstance(frame, dict) else None

    # --- subscribe (add-only) ------------------------------------------------
    def ensure(self, symbols: Iterable[str]) -> Dict[str, dict]:
        """Ask the feed worker to subscribe ``symbols`` (idempotent, add-only).

        Publishes a request on the control channel and waits for the worker's
        ``ack`` on the data channel. The ack carries the last tick per symbol
        (the snapshot), so a strategy joining mid-session has current state on
        return. Raises :class:`TickFeedError` on a rejected request or a missing
        ack — a strategy must treat that as "no data", not retry blindly.
        """
        wanted = [str(s).strip() for s in symbols if str(s).strip()]
        if not wanted:
            raise TickFeedError("no symbols given to ensure()")
        request_id = uuid.uuid4().hex
        payload = {"action": "subscribe", "symbols": wanted,
                   "request_id": request_id}
        published = False
        try:
            self._client.publish(self.control_channel, json.dumps(payload))
            published = True
        except Exception:  # noqa: BLE001 — Redis blip: a clear, retryable error
            raise TickFeedError("could not reach the tick gateway "
                                "(Redis unavailable)") from None
        if not published:
            raise TickFeedError("the tick gateway refused the request")
        deadline = time.monotonic() + self._ack_timeout
        while time.monotonic() < deadline:
            frame = self._next_frame(timeout=1.0)
            if frame is None:
                continue
            self._note_liveness(frame)
            if frame.get("type") != "ack":
                continue  # a tick/heartbeat/other ack — not for this request
            if frame.get("request_id") != request_id:
                continue
            error = frame.get("error")
            if error:
                raise TickFeedError(f"subscription rejected: {error}")
            self._snapshot = frame.get("snapshot") or {}
            self._last_frame_wall = time.monotonic()
            return self._snapshot
        raise TickFeedError(
            f"no ack for subscription within {self._ack_timeout:.0f}s — the "
            "feed worker may be down or the symbols were rejected")

    @property
    def snapshot(self) -> Dict[str, dict]:
        """The last tick per symbol, as of the most recent ``ensure()`` call."""
        return dict(self._snapshot)

    # --- the stream ----------------------------------------------------------
    def stream(self, max_age: float = 10.0) -> Iterator[dict]:
        """Yield ticks for the account's feed, staleness-enforced.

        Blocks between ticks (a quiet market yields nothing until it trades).
        A tick whose own timestamp is older than ``max_age`` seconds, or a
        liveness gap past the feed's heartbeat cadence, raises
        :class:`TickStaleError` instead of yielding — the "never trade on a
        dead feed" guarantee. Heartbeat and feed-state frames keep liveness
        alive (they do not yield: they are not trades).
        """
        last_wall: Optional[float] = None
        while True:
            frame = self._next_frame(timeout=1.0)
            if frame is None:
                # No frame this cycle: a sustained liveness gap is a dead feed.
                if last_wall is not None and (time.monotonic() - last_wall) > max_age:
                    raise TickStaleError(
                        f"no tick or heartbeat for {max_age:.0f}s on "
                        f"{self.broker} — treating the feed as stale")
                continue
            kind = frame.get("type")
            if kind == "tick":
                last_wall = time.monotonic()
                ts = frame.get("ts_gateway") or frame.get("ts_exchange")
                if ts and (time.time() - float(ts)) > max_age:
                    raise TickStaleError(
                        f"tick for {self.broker} is "
                        f"{time.time() - float(ts):.0f}s old (> {max_age:.0f}s)")
                yield frame
            else:
                # heartbeat / feed-state: liveness, not data. Keep the timer
                # alive (a closed market still heartbeats — that is healthy).
                self._note_liveness(frame)
                last_wall = time.monotonic()

    # --- liveness ------------------------------------------------------------
    def _note_liveness(self, frame: dict) -> None:
        with self._lock:
            self._last_frame_wall = time.monotonic()

    def close(self) -> None:
        """Release the Redis subscription (call at a strategy's end of run)."""
        with self._lock:
            if self._pubsub is not None:
                try:
                    self._pubsub.close()
                except Exception:  # noqa: BLE001 — best-effort
                    pass
                self._pubsub = None

    def __enter__(self) -> "TickFeed":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
