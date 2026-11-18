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
        from app.services.tick_service import TickService as _TS
        feed.set_symbols(_TS._desired(doc))
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
def test_subscribe_claims_and_persists(tick_service):
    _connect(tick_service)
    tick_service.enable("USR-1", "fyers")
    status = tick_service.subscribe("USR-1", "fyers",
                                    ["NSE:SBIN-EQ", " NSE:INFY-EQ ",
                                     "NSE:SBIN-EQ"])
    assert status.subscriptions == ["NSE:INFY-EQ", "NSE:SBIN-EQ"]
    doc = tick_service.store.get("USR-1", "fyers")
    assert set(doc.tick.claims) == {"NSE:INFY-EQ", "NSE:SBIN-EQ"}
    # The dashboard's claim is recorded with provenance…
    assert "dashboard" in doc.tick.claims["NSE:SBIN-EQ"]
    assert doc.tick.claims["NSE:SBIN-EQ"]["dashboard"].claimed_at
    # …and the union reaches the worker (FakeFeed records set_symbols).
    assert tick_service._feeds[("USR-1", "fyers")].symbols == \
        ["NSE:INFY-EQ", "NSE:SBIN-EQ"]


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


def test_two_consumers_sharing_a_symbol_keep_it_until_both_let_go(tick_service):
    """THE refcounting contract: strategy A's unsubscribe must not break
    the dashboard's (or another strategy's) stake — the symbol drops from
    the broker socket only when the LAST claim goes."""
    _connect(tick_service)
    tick_service.enable("USR-1", "fyers")
    tick_service.subscribe("USR-1", "fyers", ["NSE:SBIN-EQ"],
                           consumer="STR-004")          # a strategy claims
    tick_service.subscribe("USR-1", "fyers", ["NSE:SBIN-EQ"])  # dashboard too
    status = tick_service.status("USR-1", "fyers")
    assert status.claims == {"NSE:SBIN-EQ": ["STR-004", "dashboard"]}
    assert status.subscriptions == ["NSE:SBIN-EQ"]

    tick_service.unsubscribe("USR-1", "fyers", ["NSE:SBIN-EQ"],
                             consumer="STR-004")
    status = tick_service.status("USR-1", "fyers")
    assert status.subscriptions == ["NSE:SBIN-EQ"], "still claimed by dashboard"
    assert status.claims == {"NSE:SBIN-EQ": ["dashboard"]}
    assert tick_service._feeds[("USR-1", "fyers")].symbols == ["NSE:SBIN-EQ"]

    tick_service.unsubscribe("USR-1", "fyers", ["NSE:SBIN-EQ"])  # dashboard
    status = tick_service.status("USR-1", "fyers")
    assert status.subscriptions == []
    assert status.claims == {}
    assert tick_service._feeds[("USR-1", "fyers")].symbols == []


def test_unsubscribe_by_a_non_holder_changes_nothing(tick_service):
    """Releasing a claim you don't hold must not shrink anyone else's."""
    _connect(tick_service)
    tick_service.enable("USR-1", "fyers")
    tick_service.subscribe("USR-1", "fyers", ["NSE:SBIN-EQ"])  # dashboard
    status = tick_service.unsubscribe("USR-1", "fyers", ["NSE:SBIN-EQ"],
                                      consumer="STR-999")
    assert status.subscriptions == ["NSE:SBIN-EQ"]


def test_release_strategy_claims_on_halt(tick_service):
    """Halt = "stop this strategy NOW": its tick claims die with the process
    (the halt route calls this). Dashboard claims and OTHER strategies'
    claims are untouched; the live socket follows via the drift check."""
    _connect(tick_service)
    tick_service.enable("USR-1", "fyers")
    tick_service.subscribe("USR-1", "fyers", ["NSE:CIPLA-EQ"],
                           consumer="STR-002")
    tick_service.subscribe("USR-1", "fyers", ["NSE:SBIN-EQ"],
                           consumer="STR-009")  # another strategy
    tick_service.subscribe("USR-1", "fyers", ["NSE:INFY-EQ"])  # dashboard

    released = tick_service.release_strategy_claims("STR-002")

    assert released == ["NSE:CIPLA-EQ"]
    status = tick_service.status("USR-1", "fyers")
    assert status.claims == {"NSE:SBIN-EQ": ["STR-009"],
                             "NSE:INFY-EQ": ["dashboard"]}
    feed = tick_service._feeds[("USR-1", "fyers")]
    if feed is not None:
        assert feed.symbols == ["NSE:INFY-EQ", "NSE:SBIN-EQ"]
    # Idempotent: releasing again changes nothing.
    assert tick_service.release_strategy_claims("STR-002") == []


def test_strategy_claims_expire_after_the_ttl(tick_service, settings):
    """The GC: a halted/crashed strategy cannot unsubscribe for itself, so
    its claims are released after tick_claim_ttl_hours (the dashboard's
    claims are EXEMPT — they are the operator's standing intent). The
    release propagates: the registry shrinks and the live socket drops the
    symbol via the drift check."""
    import datetime as dt

    from app.models.broker import TickClaim

    _connect(tick_service)
    tick_service.enable("USR-1", "fyers")
    settings.tick_claim_ttl_hours = 36.0
    stale = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(hours=40)).isoformat()
    fresh = dt.datetime.now(dt.timezone.utc).isoformat()
    doc = tick_service.store.get("USR-1", "fyers")
    doc.tick.claims = {
        "NSE:OLD-EQ": {"STR-004": TickClaim(claimed_at=stale)},   # expired
        "NSE:NEW-EQ": {"STR-004": TickClaim(claimed_at=fresh)},   # alive
        "NSE:KEEP-EQ": {"dashboard": TickClaim(claimed_at=stale)},  # exempt
    }
    tick_service.store.put(doc)

    subscriptions = tick_service.subscriptions("USR-1", "fyers")

    assert subscriptions == ["NSE:KEEP-EQ", "NSE:NEW-EQ"]
    persisted = tick_service.store.get("USR-1", "fyers").tick.claims
    assert "NSE:OLD-EQ" not in persisted
    assert "STR-004" in persisted["NSE:NEW-EQ"]
    assert "dashboard" in persisted["NSE:KEEP-EQ"]
    feed = tick_service._feeds[("USR-1", "fyers")]
    if feed is not None:
        assert feed.symbols == ["NSE:KEEP-EQ", "NSE:NEW-EQ"]


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


def test_worker_credentials_are_cached_between_polls():
    """The supervision loop polls credentials every ~2 s for token rotation —
    without the TTL cache that is a network read (S3 + 2× SSM) per poll,
    ~86k store calls/day/account. The cache serves repeats; the read happens
    once per TTL, and a cache-miss path still works after expiry."""
    calls = {"n": 0}

    def counting_credentials():
        calls["n"] += 1
        return {"client_id": "APP-100", "access_token": "tok-a"}

    from app.services.tick_service import FyersTickFeed

    feed = FyersTickFeed("USR-1", "fyers", counting_credentials, RecordingBus())
    feed.set_symbols(["NSE:SBIN-EQ"])
    try:
        feed._credentials()
        feed._credentials()
        feed._credentials()
        assert calls["n"] == 1, "repeats inside the TTL must hit the cache"
        feed._creds_checked_at = 0.0  # force expiry
        feed._credentials()
        assert calls["n"] == 2, "after expiry the store is re-read"
    finally:
        feed.stop()


def test_worker_teardown_on_an_auth_error(monkeypatch):
    import app.services.tick_service as ts

    monkeypatch.setattr(ts.data_ws, "FyersDataSocket", FakeSocket)
    feed, _ = _worker(["NSE:SBIN-EQ"])
    socket = feed._build_socket()
    feed._on_error({"code": -99, "message": "Token is expired"})
    assert socket.closed is True  # rebuilt with a fresh token on the next loop


# --- the desired set is a PROJECTION of S3 ------------------------------------
def test_worker_syncs_the_fresh_desired_set_on_rebuild(monkeypatch):
    """Every socket rebuild re-reads the desired set (union of claims) from
    S3 — the live socket must project the source of truth, never a stale
    in-memory copy (regression: a behind-the-back doc edit left the socket
    subscribed to a symbol the registry no longer held, and ticks kept
    flowing for data nobody claimed)."""
    import app.services.tick_service as ts

    monkeypatch.setattr(ts.data_ws, "FyersDataSocket", FakeSocket)
    desired = {"NSE:SBIN-EQ"}

    def desired_fn():
        return sorted(desired)

    from app.services.tick_service import FyersTickFeed

    bus = RecordingBus()
    feed = FyersTickFeed("USR-1", "fyers",
                         lambda: {"client_id": "APP-100", "access_token": "t"},
                         bus, desired_fn=desired_fn)
    feed.set_symbols(["NSE:SBIN-EQ"])
    socket = feed._build_socket()
    assert socket.subscribed == ["NSE:SBIN-EQ"]

    desired.add("NSE:INFY-EQ")  # S3 changed behind the worker's back
    feed._teardown()            # (e.g. token rotation forces a rebuild)
    socket2 = feed._build_socket()
    assert socket2.subscribed == ["NSE:INFY-EQ", "NSE:SBIN-EQ"]
    assert feed._symbols == ["NSE:INFY-EQ", "NSE:SBIN-EQ"]


def test_worker_resyncs_drift_within_the_supervision_loop(monkeypatch):
    """The periodic drift check: S3's desired set changed behind the worker's
    back → the socket is corrected WITHOUT waiting for a rebuild."""
    import app.services.tick_service as ts

    monkeypatch.setattr(ts.data_ws, "FyersDataSocket", FakeSocket)
    desired = {"NSE:SBIN-EQ"}

    from app.services.tick_service import FyersTickFeed

    feed = FyersTickFeed("USR-1", "fyers",
                         lambda: {"client_id": "APP-100", "access_token": "t"},
                         RecordingBus(), desired_fn=lambda: sorted(desired))
    feed.set_symbols(["NSE:SBIN-EQ"])
    socket = feed._build_socket()
    assert feed._symbols == ["NSE:SBIN-EQ"]

    desired.clear()  # the registry dropped the symbol behind our back
    feed._check_drift()
    assert feed._symbols == []
    assert socket.unsubscribed == ["NSE:SBIN-EQ"]


def test_worker_keeps_the_last_set_when_the_desired_read_fails():
    """A store hiccup during a drift check must NOT unsub everything —
    the last known set is kept until the read recovers."""
    from app.services.tick_service import FyersTickFeed

    calls = {"n": 0}

    def broken_desired():
        calls["n"] += 1
        raise RuntimeError("s3 down")

    feed = FyersTickFeed("USR-1", "fyers",
                         lambda: {"client_id": "APP-100", "access_token": "t"},
                         RecordingBus(), desired_fn=broken_desired)
    feed.set_symbols(["NSE:SBIN-EQ"])
    feed._check_drift()
    assert feed._symbols == ["NSE:SBIN-EQ"], "the last known set survives"


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
        feed, {"action": "prune", "symbols": ["NSE:SBIN-EQ"]})["error"]
    assert "no symbols" in tick_service.handle_control_request(
        feed, {"action": "subscribe", "symbols": []})["error"]
    assert "no symbols" in tick_service.handle_control_request(
        feed, {"action": "subscribe", "symbols": "NSE:SBIN-EQ"})["error"]
    assert "invalid symbol" in tick_service.handle_control_request(
        feed, {"action": "subscribe", "symbols": ["SBIN"]})["error"]


def test_control_unsubscribe_persists_and_acks(tick_service):
    feed = _armed_control(tick_service)
    tick_service.handle_control_request(
        feed, {"action": "subscribe",
               "symbols": ["NSE:SBIN-EQ", "NSE:INFY-EQ"],
               "request_id": "r-1"})
    assert tick_service.subscriptions("USR-1", "fyers") == \
        ["NSE:INFY-EQ", "NSE:SBIN-EQ"]

    ack = tick_service.handle_control_request(
        feed, {"action": "unsubscribe", "symbols": ["NSE:SBIN-EQ"],
               "request_id": "r-2"})
    assert ack["type"] == "ack" and ack["action"] == "unsubscribe"
    assert ack["error"] is None
    assert ack["removed"] == ["NSE:SBIN-EQ"]
    assert ack["subscriptions"] == ["NSE:INFY-EQ"]
    # Persisted: the durable desired set shrank…
    assert tick_service.subscriptions("USR-1", "fyers") == ["NSE:INFY-EQ"]
    # …and the worker was told (the live socket unsubscribes the symbol).
    assert feed.symbols == ["NSE:INFY-EQ"]


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
