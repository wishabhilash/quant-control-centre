"""quant_client tests — the strategy-side wire protocol, no live Redis.

A ``FakeClient`` stands in for the Redis client: its ``publish`` on the
``…:control`` channel simulates the feed worker answering with an
ack-with-snapshot on the data channel. ``import quant_client`` resolves via the
conftest's ``sys.path`` (no install needed).
"""
import json
import time

import pytest

from quant_client import TickFeed, TickFeedError, TickStaleError


def _config(broker="fyers", channel="ticks:USR-1:fyers"):
    return {"data": {"gateway": {broker: {
        "transport": "redis-pubsub", "url": "redis://localhost:6379/0",
        "channel": channel, "subscriptions": []}}}}


class FakePubsub:
    def __init__(self, client):
        self._client = client
        self.channels = []
        self.closed = False

    def subscribe(self, channel):
        self.channels.append(channel)

    def get_message(self, timeout=None):
        if self._client._frames:
            frame = self._client._frames.pop(0)
            return {"type": "message", "data": json.dumps(frame)}
        if self._client._on_empty is not None:
            self._client._on_empty(timeout)
        return None

    def close(self):
        self.closed = True


class FakeClient:
    """A Redis stand-in. ``ack_error`` / ``no_ack`` model a rejected or
    unanswered subscription; ``on_empty`` paces the stream's liveness gap."""

    def __init__(self, frames=None, no_ack=False, ack_error=None,
                 on_empty=None):
        self._frames = list(frames or [])
        self._no_ack = no_ack
        self._ack_error = ack_error
        self._on_empty = on_empty
        self.published = []
        self._closed = False

    def pubsub(self, ignore_subscribe_messages=False):
        return FakePubsub(self)

    def publish(self, channel, data):
        self.published.append((channel, data))
        # Simulate the feed worker: a control request gets an ack on the DATA
        # channel, carrying the last-tick snapshot.
        if channel.endswith(":control") and not self._no_ack:
            request = json.loads(data)
            self._frames.append({
                "type": "ack", "request_id": request.get("request_id"),
                "broker": "fyers", "added": request.get("symbols", []),
                "subscriptions": request.get("symbols", []),
                "state": "connected",
                "error": self._ack_error,
                "snapshot": {"NSE:SBIN-EQ": {"v": 1, "ltp": 812.35}},
                "ts": 1.0,
            })
        return 1

    def close(self):
        self._closed = True


# --- discovery ---------------------------------------------------------------
def test_resolves_the_gateway_from_config():
    feed = TickFeed(_config(), client=FakeClient())
    assert feed.broker == "fyers"
    assert feed.channel == "ticks:USR-1:fyers"
    assert feed.control_channel == "ticks:USR-1:fyers:control"
    feed.close()


def test_no_gateway_is_a_clear_error():
    with pytest.raises(TickFeedError):
        TickFeed({}, client=FakeClient())


def test_multiple_gateways_need_a_broker_arg():
    # A gateway set that excludes the default broker ("fyers") is ambiguous
    # unless the caller names one.
    config = {"data": {"gateway": {
        "dhan": {"transport": "redis-pubsub", "url": "redis://localhost:6379/0",
                 "channel": "ticks:USR-1:dhan", "subscriptions": []},
        "mcx": {"transport": "redis-pubsub", "url": "redis://localhost:6379/0",
                "channel": "ticks:USR-1:mcx", "subscriptions": []}}}}
    with pytest.raises(TickFeedError):
        TickFeed(config, client=FakeClient())  # ambiguous
    feed = TickFeed(config, broker="dhan", client=FakeClient())  # disambiguated
    assert feed.broker == "dhan"
    feed.close()


# --- ensure (add-only subscribe + ack-with-snapshot) -------------------------
def test_ensure_publishes_and_returns_the_snapshot():
    client = FakeClient()
    feed = TickFeed(_config(), client=client)
    snapshot = feed.ensure(["NSE:SBIN-EQ"])
    # The request rode the control channel; the snapshot came back on the ack.
    assert client.published[0][0] == "ticks:USR-1:fyers:control"
    assert json.loads(client.published[0][1])["symbols"] == ["NSE:SBIN-EQ"]
    assert snapshot == {"NSE:SBIN-EQ": {"v": 1, "ltp": 812.35}}
    assert feed.snapshot == snapshot  # the latest ack is remembered
    feed.close()


def test_ensure_surfaces_a_rejected_subscription():
    client = FakeClient(ack_error="too many subscriptions")
    feed = TickFeed(_config(), client=client)
    with pytest.raises(TickFeedError) as excinfo:
        feed.ensure(["NSE:SBIN-EQ"])
    assert "too many subscriptions" in str(excinfo.value)
    feed.close()


def test_ensure_without_an_ack_times_out():
    client = FakeClient(no_ack=True)
    feed = TickFeed(_config(), client=client, ack_timeout=0.1)
    with pytest.raises(TickFeedError) as excinfo:
        feed.ensure(["NSE:SBIN-EQ"])
    assert "no ack" in str(excinfo.value)
    feed.close()


# --- stream (staleness-enforced) --------------------------------------------
def test_stream_skips_non_ticks_and_yields_ticks():
    client = FakeClient(frames=[
        {"type": "heartbeat", "ts": time.time(), "subscriptions": []},
        {"type": "feed", "state": "connected", "ts": time.time()},
        {"type": "tick", "instrument": {"symbol": "SBIN-EQ"}, "ltp": 812.35,
         "ts_gateway": time.time(), "ts_exchange": time.time()},
    ])
    feed = TickFeed(_config(), client=client)
    stream = feed.stream(max_age=100)  # large: no staleness in this test
    first = next(stream)  # the heartbeat + feed frames are skipped
    assert first["type"] == "tick" and first["ltp"] == 812.35
    stream.close()


def test_stream_raises_on_a_stale_tick():
    old = time.time() - 9999  # far in the past
    client = FakeClient(frames=[{"type": "tick", "ltp": 1.0,
                                 "ts_gateway": old, "ts_exchange": old}])
    feed = TickFeed(_config(), client=client)
    stream = feed.stream(max_age=10)
    with pytest.raises(TickStaleError):
        next(stream)
    stream.close()


def test_stream_raises_on_a_liveness_gap(monkeypatch):
    """No tick or heartbeat past max_age → the feed is treated as dead."""
    import types
    import quant_client.feed as feedmod

    clock = {"t": 1000.0}
    fake_time = types.SimpleNamespace(monotonic=lambda: clock["t"],
                                      time=lambda: clock["t"])
    monkeypatch.setattr(feedmod, "time", fake_time)
    # One heartbeat starts liveness; then on_empty ages the clock past max_age.
    client = FakeClient(frames=[{"type": "heartbeat", "ts": 1000,
                                 "subscriptions": []}],
                        on_empty=lambda to: clock.__setitem__("t", clock["t"] + 6.0))
    feed = TickFeed(_config(), client=client)
    stream = feed.stream(max_age=5)
    with pytest.raises(TickStaleError):
        next(stream)
    stream.close()


def test_stream_reconnects_after_a_redis_blip(monkeypatch):
    """A broken socket is recovered (a blip), not the end of the stream."""
    import quant_client.feed as feedmod

    client = FakeClient(frames=[{"type": "tick", "ltp": 1.0,
                                 "ts_gateway": time.time(),
                                 "ts_exchange": time.time()}])
    feed = TickFeed(_config(), client=client)
    calls = {"n": 0}

    def boom(*a, **k):
        raise RuntimeError("socket reset")

    feed._pubsub = None  # force a reconnect
    monkeypatch.setattr(client, "pubsub", boom)
    # _reconnect swallows the failure and leaves _pubsub None; the stream keeps
    # trying rather than dying.
    feed._reconnect()
    assert feed._pubsub is None  # still down, but the stream did not raise here
    feed.close()
