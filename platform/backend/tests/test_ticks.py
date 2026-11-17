"""Tick-data feeds: per-account gateway state, subscriptions and the bus.

Offline: the feed worker is a fake (no socket, no threads left running), the
bus is the real TickBus pointed at an unreachable port (publish must never
raise) — no Fyers network, no Redis required (§7).
"""
import pytest

from app.models.broker import BrokerDocument, TickSubscribeRequest
from app.services.tick_bus import TickBus, channel_for
from app.services.tick_service import (TICK_FEEDS, TickError, TickService,
                                       _normalize_symbol, _normalize_tick)

from tests.test_brokers import FakeSecretStore, SpyScheduler


class FakeFeed:
    """Stand-in for FyersTickFeed: no threads, records what it is told."""

    def __init__(self, doc):
        self.user_id, self.broker = doc.user_id, doc.broker
        self.started = False
        self.stopped = False
        self.symbols: list = []
        self.last_ticks: dict = {}
        self.snap = {"state": "connected", "last_error": None,
                     "ticks_published": 3, "last_tick_at": None,
                     "last_connect_at": None, "ages": {}}

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def is_alive(self):
        return self.started and not self.stopped

    def join(self, timeout=0):
        pass

    def set_symbols(self, symbols):
        self.symbols = list(symbols)

    def snapshot(self):
        return dict(self.snap)

    def snapshot_ticks(self):
        return dict(self.last_ticks)


@pytest.fixture
def tick_service(settings, fake_s3):
    from app.storage.broker_store import BrokerStore
    from app.services.broker_service import BrokerService

    broker_svc = BrokerService(settings, BrokerStore(fake_s3),
                               FakeSecretStore(), SpyScheduler())
    svc = TickService(settings, BrokerStore(fake_s3), broker_svc,
                      bus=TickBus(settings))
    feeds = []

    def make(doc):
        feed = FakeFeed(doc)
        feed.set_symbols(doc.tick.subscriptions or [])
        feeds.append(feed)
        return feed

    svc._make_feed = make  # type: ignore[method-assign]
    yield svc
    svc.shutdown()


def _connect(svc, user_id="USR-1", broker="fyers"):
    """A connected broker document in the store (status only — no secrets)."""
    svc.store.put(BrokerDocument(user_id=user_id, broker=broker,
                                 status="connected", enabled_at="2026-11-17T00:00:00+00:00"))


# --- symbol validation -------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("NSE:SBIN-EQ", "NSE:SBIN-EQ"),
    ("  NSE:SBIN-EQ  ", "NSE:SBIN-EQ"),
    ("NSE:NIFTY50 EQL WGT-INDEX", "NSE:NIFTY50 EQL WGT-INDEX"),
    ("MCX:SILVER26AUGFUT", "MCX:SILVER26AUGFUT"),
])
def test_valid_symbols(raw, expected):
    assert _normalize_symbol(raw) == expected


@pytest.mark.parametrize("raw", ["", "SBIN", "NSE:", ":SBIN", "a:b;c",
                                 "NSE:SBIN;DROP", "x" * 70])
def test_invalid_symbols(raw):
    with pytest.raises(TickError):
        _normalize_symbol(raw)


# --- the tick contract -------------------------------------------------------
def test_tick_contract_shape():
    tick = _normalize_tick(
        {"symbol": "NSE:SBIN-EQ", "type": "sf", "ltp": 812.35,
         "last_traded_qty": 10, "open_price": 805, "high_price": 815,
         "low_price": 802, "prev_close_price": 809.5,
         "vol_traded_today": 1250000, "avg_trade_price": 810.2,
         "bid_price": 812.3, "bid_size": 500, "ask_price": 812.4,
         "ask_size": 300, "exch_feed_time": 1763356800},
        "fyers", 1763356800.5)
    assert tick["v"] == 1 and tick["type"] == "tick" and tick["broker"] == "fyers"
    assert tick["instrument"] == {"exchange": "NSE", "symbol": "SBIN-EQ"}
    assert tick["ltp"] == 812.35 and tick["ltq"] == 10.0
    assert tick["ohlcv"]["volume"] == 1250000.0
    assert tick["top"] == {"bid": 812.3, "bid_qty": 500.0,
                           "ask": 812.4, "ask_qty": 300.0}
    assert tick["oi"] is None  # the Fyers SDK drops OI — null, never zero
    assert tick["ts_exchange"] == 1763356800.0
    assert tick["ts_gateway"] == 1763356800.5
    assert tick["depth"] is None


def test_tick_from_a_control_frame_is_none():
    # Auth/subscribe acks have no symbol — they are not ticks.
    assert _normalize_tick({"type": "sf", "s": "ok"}, "fyers", 0.0) is None


# --- enable / disable --------------------------------------------------------
def test_enable_requires_a_connected_broker(tick_service):
    with pytest.raises(TickError):
        tick_service.enable("USR-1", "fyers")
    tick_service.store.put(BrokerDocument(user_id="USR-1", broker="fyers",
                                          status="connecting"))
    with pytest.raises(TickError):
        tick_service.enable("USR-1", "fyers")


def test_enable_persists_and_starts_the_worker(tick_service):
    _connect(tick_service)
    status = tick_service.enable("USR-1", "fyers")
    assert status.enabled and status.state == "connected"
    doc = tick_service.store.get("USR-1", "fyers")
    assert doc.tick.enabled and doc.tick.enabled_at
    assert tick_service._feeds[("USR-1", "fyers")].started


def test_enable_refuses_unsupported_brokers(tick_service):
    _connect(tick_service, broker="dhan")
    with pytest.raises(TickError):
        tick_service.enable("USR-1", "dhan")
    assert "dhan" not in TICK_FEEDS


def test_disable_stops_the_worker_and_clears_enabled(tick_service):
    _connect(tick_service)
    tick_service.enable("USR-1", "fyers")
    status = tick_service.disable("USR-1", "fyers")
    assert not status.enabled and status.state is None
    assert tick_service._feeds.get(("USR-1", "fyers")) is None
    doc = tick_service.store.get("USR-1", "fyers")
    assert doc.tick.enabled is False


def test_status_when_never_enabled(tick_service):
    _connect(tick_service)
    status = tick_service.status("USR-1", "fyers")
    assert not status.enabled and status.state is None


# --- subscriptions -----------------------------------------------------------
def test_subscribe_validates_and_persists(tick_service):
    _connect(tick_service)
    tick_service.enable("USR-1", "fyers")
    status = tick_service.subscribe("USR-1", "fyers",
                                    ["NSE:SBIN-EQ", " NSE:INFY-EQ ",
                                     "NSE:SBIN-EQ"])
    assert status.subscriptions == ["NSE:INFY-EQ", "NSE:SBIN-EQ"]
    doc = tick_service.store.get("USR-1", "fyers")
    assert doc.tick.subscriptions == ["NSE:INFY-EQ", "NSE:SBIN-EQ"]


def test_subscribe_rejects_bad_symbols(tick_service):
    _connect(tick_service)
    tick_service.enable("USR-1", "fyers")
    with pytest.raises(TickError):
        tick_service.subscribe("USR-1", "fyers", ["SBIN"])
    assert tick_service.status("USR-1", "fyers").subscriptions == []


def test_subscribe_enforces_the_per_account_cap(tick_service, settings):
    _connect(tick_service)
    tick_service.enable("USR-1", "fyers")
    settings.tick_max_symbols = 2
    with pytest.raises(TickError):
        tick_service.subscribe("USR-1", "fyers",
                               ["NSE:SBIN-EQ", "NSE:INFY-EQ", "NSE:TCS-EQ"])


def test_unsubscribe_updates_the_document_and_worker(tick_service):
    _connect(tick_service)
    tick_service.enable("USR-1", "fyers")
    tick_service.subscribe("USR-1", "fyers", ["NSE:SBIN-EQ", "NSE:INFY-EQ"])
    status = tick_service.unsubscribe("USR-1", "fyers", ["NSE:SBIN-EQ"])
    assert status.subscriptions == ["NSE:INFY-EQ"]
    feed = tick_service._feeds[("USR-1", "fyers")]
    assert feed.symbols == ["NSE:INFY-EQ"]


def test_unsubscribe_without_tick_data_is_a_400(tick_service):
    _connect(tick_service)
    with pytest.raises(TickError):
        tick_service.unsubscribe("USR-1", "fyers", ["NSE:SBIN-EQ"])


# --- startup reconcile -------------------------------------------------------
def test_reconcile_rearms_enabled_feeds(tick_service):
    _connect(tick_service)
    tick_service.enable("USR-1", "fyers")
    tick_service.subscribe("USR-1", "fyers", ["NSE:SBIN-EQ"])
    feeds = list(tick_service._feeds.values())
    for feed in feeds:
        feed.started = False  # simulate "died with the old process"
    tick_service._feeds.clear()
    tick_service.reconcile_on_startup()
    assert len(tick_service._feeds) == 1
    feed = next(iter(tick_service._feeds.values()))
    assert feed.started and feed.symbols == ["NSE:SBIN-EQ"]


def test_reconcile_skips_disabled_feeds(tick_service):
    tick_service.store.put(BrokerDocument(
        user_id="USR-2", broker="fyers", status="connected"))
    tick_service.reconcile_on_startup()
    assert tick_service._feeds == {}


# --- the bus -----------------------------------------------------------------
def test_publish_survives_an_unreachable_redis(settings):
    # Port 1 on localhost — nothing listens there; publish must not raise.
    bus = TickBus(settings.model_copy(update={"redis_url": "redis://localhost:1/0"}))
    assert bus.publish(channel_for("USR-1", "fyers"),
                       {"type": "tick"}) is False


def test_channel_is_per_account():
    assert channel_for("USR-1", "fyers") == "ticks:USR-1:fyers"
    assert channel_for("USR-1", "fyers") != channel_for("USR-2", "fyers")


# --- the worker (callbacks driven directly — no threads, no socket) ----------
class RecordingBus:
    def __init__(self):
        self.published: list = []

    def publish(self, channel, payload):
        self.published.append((channel, payload))
        return True


class FakeSocket:
    """Stand-in for the vendored FyersDataSocket."""

    def __init__(self, access_token, reconnect=True, reconnect_retry=10,
                 on_connect=None, on_message=None, on_error=None,
                 on_close=None):
        self.access_token = access_token
        self.on_connect, self.on_message = on_connect, on_message
        self.on_error, self.on_close = on_error, on_close
        self.connected = False
        self.closed = False
        self.subscribed: list = []
        self.unsubscribed: list = []

    def connect(self):
        self.connected = True
        self.on_connect()

    def is_connected(self):
        return self.connected and not self.closed

    def close_connection(self):
        self.closed = True

    def subscribe(self, symbols, data_type=None):
        self.subscribed.extend(symbols)

    def unsubscribe(self, symbols, data_type=None):
        self.unsubscribed.extend(symbols)


def _worker(symbols, token="tok-a"):
    from app.services.tick_service import FyersTickFeed

    bus = RecordingBus()
    feed = FyersTickFeed("USR-1", "fyers", lambda: {"client_id": "APP-100",
                                                    "access_token": token}, bus)
    feed.set_symbols(list(symbols))
    return feed, bus


def test_worker_subscribes_the_desired_set_on_connect(monkeypatch):
    import app.services.tick_service as ts

    monkeypatch.setattr(ts.data_ws, "FyersDataSocket", FakeSocket)
    feed, _ = _worker(["NSE:SBIN-EQ", "NSE:INFY-EQ"])
    socket = feed._build_socket()
    assert socket.subscribed == ["NSE:SBIN-EQ", "NSE:INFY-EQ"]
    assert feed.snapshot()["state"] == "connected"


def test_worker_publishes_normalized_ticks(monkeypatch):
    import app.services.tick_service as ts

    monkeypatch.setattr(ts.data_ws, "FyersDataSocket", FakeSocket)
    feed, bus = _worker(["NSE:SBIN-EQ"])
    socket = feed._build_socket()
    feed._on_message({"symbol": "NSE:SBIN-EQ", "type": "sf", "ltp": 812.35,
                      "exch_feed_time": 1763356800})
    kind, payload = bus.published[-1]
    assert kind == "ticks:USR-1:fyers"
    assert payload["type"] == "tick" and payload["ltp"] == 812.35
    assert payload["instrument"]["symbol"] == "SBIN-EQ"
    assert feed.snapshot()["ticks_published"] == 1


def test_worker_drops_ticks_for_symbols_no_longer_desired(monkeypatch):
    import app.services.tick_service as ts

    monkeypatch.setattr(ts.data_ws, "FyersDataSocket", FakeSocket)
    feed, bus = _worker(["NSE:SBIN-EQ"])
    feed._build_socket()
    feed.set_symbols([])  # nothing desired anymore
    before = len(bus.published)
    feed._on_message({"symbol": "NSE:SBIN-EQ", "type": "sf", "ltp": 1})
    assert len(bus.published) == before


def test_worker_teardown_on_an_auth_error(monkeypatch):
    import app.services.tick_service as ts

    monkeypatch.setattr(ts.data_ws, "FyersDataSocket", FakeSocket)
    feed, _ = _worker(["NSE:SBIN-EQ"])
    socket = feed._build_socket()
    feed._on_error({"code": -99, "message": "Token is expired"})
    assert socket.closed is True  # rebuilt with a fresh token on the next loop


# --- the strategy contract ---------------------------------------------------
def test_data_section_reports_enabled_feeds_only(tick_service):
    from app.services.tick_bus import channel_for

    _connect(tick_service)
    assert tick_service.data_section("USR-1") == {}  # not enabled yet
    tick_service.enable("USR-1", "fyers")
    tick_service.subscribe("USR-1", "fyers", ["NSE:SBIN-EQ"])
    section = tick_service.data_section("USR-1")
    gw = section["gateway"]["fyers"]
    assert gw["transport"] == "redis-pubsub"
    assert gw["channel"] == channel_for("USR-1", "fyers")
    assert gw["subscriptions"] == ["NSE:SBIN-EQ"]
    assert gw["url"] == tick_service.settings.redis_url
    tick_service.disable("USR-1", "fyers")
    assert tick_service.data_section("USR-1") == {}
    assert tick_service.data_section("USR-NOPE") == {}
    assert tick_service.data_section(None) == {}


def test_runtime_config_carries_the_tick_gateway(strategy_service, settings,
                                                 fake_s3):
    """The runner-injected runtime config points the strategy at its
    account's tick channel (config["data"]["gateway"]["fyers"])."""
    import json

    from app.services.process_service import ProcessService
    from app.storage.broker_store import BrokerStore
    from tests.test_brokers import _deployed_owned

    broker_doc = BrokerDocument(user_id="USR-TICKS", broker="fyers",
                                status="connected")
    fake_s3.objects[f"brokers/USR-TICKS/fyers.json"] = {
        "body": broker_doc.model_dump_json().encode("utf-8"),
        "etag": '"etag-1"',
    }
    tick = TickService(settings, BrokerStore(fake_s3), None, bus=TickBus(settings))
    tick.store.put(broker_doc)
    tick.enable("USR-TICKS", "fyers")
    tick.subscribe("USR-TICKS", "fyers", ["NSE:SBIN-EQ"])

    sid = _deployed_owned(strategy_service, settings, "USR-TICKS")
    proc = ProcessService(settings, strategy_service, None, tick)
    doc = strategy_service.get(sid)
    proc._write_runtime_config(doc)
    payload = json.loads((settings.strategies_dir_path() / sid / ".venv"
                          / "share" / "runtime_config.json").read_text())
    gw = payload["data"]["gateway"]["fyers"]
    assert gw["channel"] == "ticks:USR-TICKS:fyers"
    assert gw["subscriptions"] == ["NSE:SBIN-EQ"]
    assert payload["data"]["gateway"]["fyers"]["transport"] == "redis-pubsub"


def test_runtime_config_without_tick_service_has_no_data_key(strategy_service,
                                                             settings):
    import json

    from app.services.process_service import ProcessService
    from tests.test_brokers import _deployed_owned

    sid = _deployed_owned(strategy_service, settings, "USR-NOFEED")
    proc = ProcessService(settings, strategy_service)  # no tick service at all
    doc = strategy_service.get(sid)
    proc._write_runtime_config(doc)
    payload = json.loads((settings.strategies_dir_path() / sid / ".venv"
                          / "share" / "runtime_config.json").read_text())
    assert payload["data"] == {}


# --- the control channel (quant_client ↔ worker) ------------------------------
def _armed_control(svc, user_id="USR-1", broker="fyers"):
    """A connected + enabled feed whose control handler is wired (what
    `_ensure_worker` does for a real FyersTickFeed)."""
    _connect(svc, user_id, broker)
    svc.enable(user_id, broker)
    feed = svc._feed(user_id, broker)
    feed._control_handler = (lambda request:
                             svc.handle_control_request(feed, request))
    return feed


def test_control_subscribe_persists_and_acks(tick_service):
    feed = _armed_control(tick_service)
    feed.last_ticks = {"NSE:SBIN-EQ": {"v": 1, "ltp": 812.35}}
    ack = tick_service.handle_control_request(
        feed, {"action": "subscribe", "symbols": ["NSE:SBIN-EQ", "NSE:INFY-EQ"],
               "request_id": "req-1"})
    # Persisted: the desired set now lives in the S3 document.
    assert tick_service.subscriptions("USR-1", "fyers") == \
        ["NSE:INFY-EQ", "NSE:SBIN-EQ"]
    assert feed.symbols == ["NSE:INFY-EQ", "NSE:SBIN-EQ"]
    assert ack["type"] == "ack" and ack["request_id"] == "req-1"
    assert ack["error"] is None
    assert ack["added"] == ["NSE:INFY-EQ", "NSE:SBIN-EQ"]
    assert ack["snapshot"] == {"NSE:SBIN-EQ": {"v": 1, "ltp": 812.35}}
    # Re-subscribing an already-desired symbol adds nothing (add-only, idempotent).
    ack = tick_service.handle_control_request(
        feed, {"action": "subscribe", "symbols": ["NSE:SBIN-EQ"],
               "request_id": "req-2"})
    assert ack["added"] == []


def test_control_subscribe_rejects_a_disabled_feed(tick_service):
    feed = _armed_control(tick_service)
    tick_service.disable("USR-1", "fyers")
    ack = tick_service.handle_control_request(
        feed, {"action": "subscribe", "symbols": ["NSE:SBIN-EQ"],
               "request_id": "req-3"})
    assert ack["type"] == "ack"
    assert "not enabled" in (ack["error"] or "")


def test_control_rejects_unsupported_actions_and_bad_symbols(tick_service):
    feed = _armed_control(tick_service)
    assert "unsupported control action" in tick_service.handle_control_request(
        feed, {"action": "unsubscribe", "symbols": ["NSE:SBIN-EQ"]})["error"]
    assert "no symbols" in tick_service.handle_control_request(
        feed, {"action": "subscribe", "symbols": []})["error"]
    assert "no symbols" in tick_service.handle_control_request(
        feed, {"action": "subscribe", "symbols": "NSE:SBIN-EQ"})["error"]
    assert "invalid symbol" in tick_service.handle_control_request(
        feed, {"action": "subscribe", "symbols": ["SBIN"]})["error"]


def test_control_enforces_the_per_account_cap(tick_service):
    feed = _armed_control(tick_service)
    cap = tick_service.settings.tick_max_symbols
    ack = tick_service.handle_control_request(
        feed, {"action": "subscribe",
               "symbols": [f"NSE:S{i}-EQ" for i in range(cap + 1)],
               "request_id": "req-4"})
    assert f"cap is {cap}" in (ack["error"] or "")
    assert tick_service.subscriptions("USR-1", "fyers") == []


def test_worker_caches_the_last_tick_per_symbol(monkeypatch):
    import app.services.tick_service as ts

    monkeypatch.setattr(ts.data_ws, "FyersDataSocket", FakeSocket)
    feed, _ = _worker(["NSE:SBIN-EQ"])
    feed._build_socket()
    feed._on_message({"symbol": "NSE:SBIN-EQ", "type": "sf", "ltp": 812.35})
    cached = feed.snapshot_ticks()
    assert cached["NSE:SBIN-EQ"]["ltp"] == 812.35
    # Copies — a consumer mutating the snapshot must not corrupt the cache.
    cached["NSE:SBIN-EQ"]["ltp"] = 0
    assert feed.snapshot_ticks()["NSE:SBIN-EQ"]["ltp"] == 812.35
    # A symbol dropped from the desired set leaves the cache.
    feed.set_symbols([])
    assert feed.snapshot_ticks() == {}


def test_worker_control_message_dispatches_and_acks(monkeypatch):
    """_handle_control_message: parse → handler → ack published on the DATA
    channel (a strategy never talks to the service directly)."""
    import json

    import app.services.tick_service as ts

    monkeypatch.setattr(ts.data_ws, "FyersDataSocket", FakeSocket)
    feed, bus = _worker(["NSE:SBIN-EQ"])
    feed._build_socket()
    feed._on_message({"symbol": "NSE:SBIN-EQ", "type": "sf", "ltp": 812.35})
    seen: list = []

    def handler(request):
        seen.append(request)
        return {"type": "ack", "request_id": request.get("request_id")}

    feed._control_handler = handler
    feed._handle_control_message(json.dumps(
        {"action": "subscribe", "symbols": ["NSE:INFY-EQ"],
         "request_id": "req-9"}))
    assert list(seen) == [{"action": "subscribe", "symbols": ["NSE:INFY-EQ"],
                           "request_id": "req-9"}]
    channel, ack = bus.published[-1]
    assert channel == feed.channel  # acks ride the data channel
    assert ack["type"] == "ack" and ack["request_id"] == "req-9"
    # Garbage on the control channel is dropped, never fatal.
    feed._handle_control_message("not json")
    feed._handle_control_message(b"[1, 2]")
    assert len(seen) == 1
    # A handler yielding nothing publishes nothing.
    before = len(bus.published)
    feed._control_handler = lambda request: None
    feed._handle_control_message(json.dumps({"action": "subscribe"}))
    assert len(bus.published) == before


# --- guards ------------------------------------------------------------------
def test_the_registry_gate_is_the_only_broker_switch():
    # The API routes are generic and consult TICK_FEEDS — Dhan joining later
    # must not grow the route table, only this set (and its worker class).
    assert TICK_FEEDS == {"fyers"}
