"""Per-account tick-data feeds (market data gateway, v0 — Fyers only).

One feed worker per **user + broker account** owns that account's broker data
socket, subscribes the desired symbol set and publishes normalized ticks to a
transient Redis pub/sub channel (``ticks:{user}:{broker}``). Strategies (and
the UI) consume the bus; user code never opens a broker socket.

Decisions encoded here (from the data-feed design):

- **Per-account, never cross-account.** One connection per user+broker;
  account data is never fanned out to other users (broker ToS).
- **The desired subscription set lives in S3** (the broker connection
  document — golden rule 1), so the feed is re-armed from S3 on restart. The
  bus itself persists nothing.
- **The Fyers SDK is vendored and quarantined** (``app/brokers/_vendor/``) —
  its PyPI pins are hostile; the data socket needs only ``websocket-client``.
- **Timestamps on every tick**: ``ts_exchange`` (broker) and ``ts_gateway``
  (receipt). A consumer trading on stale data is the worst failure mode this
  design can have — the UI surfaces both.
- Depth (``DepthUpdate``) is a later tier: v0 streams ``SymbolUpdate``,
  which already carries LTP, OHLCV and top-of-book on Fyers.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from app.brokers._vendor.fyers import data_ws
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.broker import (BrokerDocument, TickClaim, TickFeedState,
                               TickFeedStatus)
from app.services.tick_bus import TickBus, channel_for, control_channel_for
from app.storage.broker_store import BrokerStore

log = get_logger("services.tick")

#: Brokers with a market-data gateway behind the ``/brokers/{b}/ticks`` API.
#: Dhan joins when its gateway exists — the routes are already generic.
TICK_FEEDS = {"fyers"}

MAX_SYMBOL_LEN = 64

# Fyers symbol notation: "NSE:SBIN-EQ", "NSE:NIFTY50-INDEX", "MCX:SILVER26AUGFUT"…
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9&_\-\s]+$")


class TickError(Exception):
    """A tick-feed domain error (the API maps it to 400)."""


def _normalize_symbol(raw: str) -> str:
    """Validate + normalize one Fyers-notation symbol (``EXCHANGE:SYMBOL``)."""
    symbol = (raw or "").strip()
    exchange, sep, scrip = symbol.partition(":")
    exchange, scrip = exchange.strip(), scrip.strip()
    if not sep or not exchange or not scrip:
        raise TickError(
            f"invalid symbol {raw!r} — expected Fyers notation like NSE:SBIN-EQ")
    if len(symbol) > MAX_SYMBOL_LEN:
        raise TickError(f"invalid symbol {raw!r}")
    if not _SYMBOL_RE.fullmatch(exchange) or not _SYMBOL_RE.fullmatch(scrip):
        raise TickError(f"invalid symbol {raw!r}")
    return f"{exchange}:{scrip}"


def _num(value) -> Optional[float]:
    """Best-effort numeric coercion (the SDK returns floats / ints / str)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_tick(msg: dict, broker: str, ts_gateway: float) -> Optional[dict]:
    """The unified tick contract (v1), filled from one Fyers message.

    Fyers ``SymbolUpdate`` fields (SDK-decoded): ``ltp, vol_traded_today,
    last_traded_time, exch_feed_time, bid_size, ask_size, bid_price, ask_price,
    last_traded_qty, tot_buy_qty, tot_sell_qty, avg_trade_price, low_price,
    high_price, open_price, prev_close_price``. Fields the message lacks stay
    ``null`` — never fabricated zeros. OI is unreachable through the Fyers
    SDK (it drops the field), so it stays null.
    """
    raw_symbol = str(msg.get("symbol") or "")
    if not raw_symbol:
        return None  # a control frame (auth/subscribe ack) — not a tick
    exchange, _, scrip = raw_symbol.partition(":")
    kind = str(msg.get("type") or "")

    tick: dict = {
        "v": 1,
        "type": "tick",
        "broker": broker,
        "instrument": {"exchange": exchange or None, "symbol": scrip or raw_symbol},
        "ltp": _num(msg.get("ltp")),
        "ltq": _num(msg.get("last_traded_qty")),
        "ohlcv": {
            "open": _num(msg.get("open_price")),
            "high": _num(msg.get("high_price")),
            "low": _num(msg.get("low_price")),
            "prev_close": _num(msg.get("prev_close_price")),
            "volume": _num(msg.get("vol_traded_today")),
            "avg_price": _num(msg.get("avg_trade_price")),
        },
        "top": {
            "bid": _num(msg.get("bid_price")),
            "bid_qty": _num(msg.get("bid_size")),
            "ask": _num(msg.get("ask_price")),
            "ask_qty": _num(msg.get("ask_size")),
        },
        "depth": None,
        "oi": None,
        "ts_exchange": _num(msg.get("exch_feed_time")),
        "ts_gateway": round(ts_gateway, 3),
        "raw": {"kind": kind},
    }
    return tick


class FyersTickFeed(threading.Thread):
    """One account's feed worker (owns the broker data socket).

    A supervised loop: build the socket with the current token, subscribe the
    desired set, and watch for (a) a stop, (b) a **token change** (the 08:00
    refresh rotates the token — the socket must come back with the fresh one)
    and (c) a dead socket (connect failure, auth refusal, or the SDK's
    reconnect attempts exhausted). Any of those tears the socket down and
    rebuilds it; nothing else in the platform shares this socket.
    """

    RECONNECT_AFTER_SECONDS = 20.0   # socket dead longer than this → rebuild
    RESTART_BACKOFF_SECONDS = 5.0    # after an auth refusal / churn guard
    HEARTBEAT_SECONDS = 15.0
    # Credentials are re-read over the NETWORK (S3 + 2× SSM) — so the
    # supervision loop polls a short-TTL cache, not the stores. Worst-case
    # rotation detection = TTL + 2 s poll, an eternity before the 09:15 open,
    # while the stores see ~2 reads/15 s instead of 3 reads/2 s.
    CREDENTIALS_CACHE_SECONDS = 15.0
    # The desired set is re-read from S3 on this cadence: the live socket
    # must not outlive a behind-the-back change to the source of truth
    # (golden rule 1 — S3 is the truth, the socket is a *projection* of it).
    DRIFT_CHECK_SECONDS = 60.0

    def __init__(self, user_id: str, broker: str,
                 credentials_fn: Callable[[], Optional[dict]],
                 bus: TickBus,
                 desired_fn: Optional[Callable[[], List[str]]] = None):
        super().__init__(name=f"tick-{broker}-{user_id}", daemon=True)
        self.user_id = user_id
        self.broker = broker
        self._credentials_fn = credentials_fn
        self._bus = bus
        # Fresh read of the desired set (union of claims, straight from S3) —
        # used on every socket rebuild and by the periodic drift check, so
        # the live socket always projects the source of truth.
        self._desired_fn = desired_fn
        self.channel = channel_for(user_id, broker)
        self.control_channel = control_channel_for(user_id, broker)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._socket = None
        self._symbols: List[str] = []       # desired set (subset of S3 doc)
        self._token_in_use: Optional[str] = None

        # The last tick per symbol (the v1 contract dict). Serves the control
        # channel's ack-with-snapshot: a strategy joining mid-session gets the
        # current state immediately instead of waiting for the next trade.
        # Transient by design — rebuilt by live ticks, never persisted.
        self._last_ticks: Dict[str, dict] = {}
        # Set by TickService: handles one control request → ack payload.
        self._control_handler: Optional[Callable[[dict], dict]] = None
        self._control_thread: Optional[threading.Thread] = None

        self._state = "starting"            # starting|connected|reconnecting|stopped|error
        self._last_error: Optional[str] = None
        self._ticks_published = 0
        self._last_tick_at: Optional[str] = None
        self._last_connect_at: Optional[str] = None
        self._last_seen: Dict[str, float] = {}  # symbol → gateway ts of last tick
        self._creds_cache: Optional[dict] = None
        self._creds_checked_at = 0.0            # monotonic; 0 = never checked

    # --- lifecycle -----------------------------------------------------------
    def stop(self) -> None:
        self._stop_event.set()

    def set_symbols(self, symbols: List[str]) -> None:
        """Update the desired set; diff it against the live socket."""
        with self._lock:
            previous = set(self._symbols)
            self._symbols = list(symbols)
            added = [s for s in self._symbols if s not in previous]
            removed = [s for s in previous if s not in self._symbols]
            # A symbol this account no longer wants must not linger in the
            # per-symbol caches (stale ages, a stale ack snapshot).
            for gone in removed:
                self._last_seen.pop(gone, None)
                self._last_ticks.pop(gone, None)
        socket = self._socket
        if socket is None or not socket.is_connected():
            return  # (re)connect subscribes the full set via _on_connect
        try:
            if added:
                socket.subscribe(symbols=added, data_type="SymbolUpdate")
            if removed:
                socket.unsubscribe(symbols=removed, data_type="SymbolUpdate")
        except Exception as exc:  # noqa: BLE001 — never kill the feed here
            self._record(error=f"subscription update failed ({type(exc).__name__})")

    # --- status --------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "last_error": self._last_error,
                "ticks_published": self._ticks_published,
                "last_tick_at": self._last_tick_at,
                "last_connect_at": self._last_connect_at,
                "ages": {s: round(time.time() - t, 1)
                         for s, t in self._last_seen.items()},
            }

    def snapshot_ticks(self) -> Dict[str, dict]:
        """Copies of the last tick per subscribed symbol (for ack snapshots)."""
        with self._lock:
            return {s: dict(t) for s, t in self._last_ticks.items()}

    # --- the loop ------------------------------------------------------------
    def run(self) -> None:
        self._start_control_thread()
        try:
            while not self._stop_event.is_set():
                socket = self._build_socket()
                if socket is None:
                    self._sleep(self.RESTART_BACKOFF_SECONDS)
                    continue
                self._socket = socket
                self._wait_while_connected()
                if self._stop_event.is_set():
                    self._teardown()
                    break
                # Dead socket / rotated token → rebuild on the next iteration.
                self._sleep(self.RESTART_BACKOFF_SECONDS)
        finally:
            self._stop_control_thread()

    # --- the control channel -------------------------------------------------
    def _start_control_thread(self) -> None:
        self._control_thread = threading.Thread(
            name=f"tick-control-{self.broker}-{self.user_id}",
            target=self._control_loop, daemon=True)
        self._control_thread.start()

    def _stop_control_thread(self) -> None:
        thread = self._control_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._control_thread = None

    def _control_loop(self) -> None:
        """Consume control requests from strategies (quant_client publishes on
        the ``…:control`` channel) and ack them on the data channel.

        Runs for the feed's whole lifetime — independently of the broker
        socket — so a strategy can arm subscriptions even while credentials
        are missing (the desired set persists in S3 and the feed subscribes
        when it comes back; the ack carries the current state instead of a
        snapshot). Failures are never fatal: a dead Redis just means requests
        go unanswered until it returns.
        """
        while not self._stop_event.is_set():
            try:
                pubsub = self._bus.pubsub(self.control_channel)
            except Exception as exc:  # noqa: BLE001 — retry until Redis returns
                self._record(error=f"control channel unavailable "
                                   f"({type(exc).__name__})")
                if self._sleep(2.0):
                    return
                continue
            try:
                while not self._stop_event.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if not message or message.get("type") != "message":
                        continue
                    self._handle_control_message(message["data"])
            except Exception as exc:  # noqa: BLE001 — reconnect on Redis blips
                self._record(error=f"control channel error ({type(exc).__name__})")
            finally:
                try:
                    pubsub.close()
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass
            if self._sleep(1.0):
                return

    def _handle_control_message(self, raw) -> None:
        try:
            request = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(request, dict):
            return
        handler = self._control_handler
        if handler is None:
            return
        try:
            ack = handler(request)
        except Exception as exc:  # noqa: BLE001 — a broken ack must not kill
            ack = {"type": "ack", "request_id": request.get("request_id"),
                   "error": f"internal error ({type(exc).__name__})"}
        if isinstance(ack, dict):
            self._publish(ack)

    # --- the loop helpers ----------------------------------------------------

    def _sleep(self, seconds: float) -> None:
        self._stop_event.wait(seconds)

    def _credentials(self) -> Optional[dict]:
        """The account's current credentials, served from a short-TTL cache.

        The supervision loop polls this every ~2 s for token rotation; without
        the cache that would be a network read (S3 + 2× SSM) per poll —
        ~86k store calls/day/account for a check that matters twice a day.
        Failures are cached too, so a store outage is retried at the same
        gentle cadence instead of hammering."""
        now = time.monotonic()
        with self._lock:
            if now - self._creds_checked_at < self.CREDENTIALS_CACHE_SECONDS:
                return self._creds_cache
        try:
            creds = self._credentials_fn()
        except Exception as exc:  # noqa: BLE001 — surfaced, not raised
            self._record(error=f"credentials unavailable ({type(exc).__name__})",
                         state="error")
            creds = None
        with self._lock:
            self._creds_cache = creds
            self._creds_checked_at = now
        return creds

    def _build_socket(self) -> Optional[data_ws.FyersDataSocket]:
        creds = self._credentials()
        if not creds or not creds.get("access_token"):
            self._record(error="no connected Fyers credentials — the token "
                               "is missing or the broker is disconnected",
                         state="error")
            return None
        self._token_in_use = creds["access_token"]
        socket = data_ws.FyersDataSocket(
            access_token=f"{creds.get('client_id', '')}:{creds['access_token']}",
            reconnect=True,
            reconnect_retry=10,
            on_connect=self._on_connect,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._record(state="starting", last_connect_at=None)
        # Assign BEFORE connect(): the SDK fires on_connect synchronously
        # inside connect() (after a ~2 s open wait) — the callback must see
        # the socket to push the initial subscription.
        self._socket = socket
        try:
            socket.connect()  # non-blocking; on_connect fires when open
        except Exception as exc:  # noqa: BLE001 — reported via on_error too
            self._socket = None
            self._record(error=f"connect failed ({type(exc).__name__})",
                         state="error")
            return None
        return socket

    def _wait_while_connected(self) -> None:
        """Supervise one socket until stop / token rotation / death."""
        dead_since: Optional[float] = None
        last_heartbeat = 0.0
        last_drift = time.monotonic()
        while not self._stop_event.is_set():
            self._sleep(2.0)
            if self._stop_event.is_set():
                return
            socket = self._socket
            if socket is None:
                return
            # A token refresh (08:00 job) rotates the credential — rebuild.
            creds = self._credentials()
            if creds and creds.get("access_token") != self._token_in_use:
                log.info("tick feed %s/%s: token rotated — rebuilding the socket",
                         self.user_id, self.broker)
                self._teardown()
                return
            connected = bool(creds) and socket.is_connected()
            if connected:
                dead_since = None
                self._record(state="connected")
            else:
                if dead_since is None:
                    dead_since = time.monotonic()
                    self._record(state="reconnecting")
                elif time.monotonic() - dead_since > self.RECONNECT_AFTER_SECONDS:
                    log.warning("tick feed %s/%s: socket did not come back — "
                                "rebuilding", self.user_id, self.broker)
                    self._teardown()
                    return
            if time.time() - last_heartbeat >= self.HEARTBEAT_SECONDS:
                last_heartbeat = time.time()
                self._publish_heartbeat()
            if time.monotonic() - last_drift >= self.DRIFT_CHECK_SECONDS:
                last_drift = time.monotonic()
                self._check_drift()

    def _teardown(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                socket.close_connection()
            except Exception:  # noqa: BLE001 — teardown must never raise
                pass

    def _current_desired(self) -> List[str]:
        """The desired set, read fresh (S3) with the in-memory list as the
        failure fallback — a store hiccup must not unsub everything."""
        if self._desired_fn is not None:
            try:
                return list(self._desired_fn())
            except Exception as exc:  # noqa: BLE001 — keep the last known set
                self._record(error=f"desired set unavailable "
                                   f"({type(exc).__name__})")
        return self._symbols

    # --- subscription sync ---------------------------------------------------
    def _sync_subscription(self) -> None:
        """Push the FULL desired set (used on every connect — the SDK resets
        its live subscription state on each reconnect, so the full push is
        what re-subscribes everything; idempotent server-side). The set is
        read FRESH from S3 so every rebuild projects the source of truth."""
        socket = self._socket
        if socket is None:
            return
        desired = self._current_desired()
        with self._lock:
            self._symbols = list(desired)
        try:
            if desired:
                socket.subscribe(symbols=desired, data_type="SymbolUpdate")
        except Exception as exc:  # noqa: BLE001 — never kill the feed here
            self._record(error=f"subscribe failed ({type(exc).__name__})")

    def _check_drift(self) -> None:
        """Resync the live socket when S3's desired set drifted from it —
        behind-the-back edits to the source of truth must reach the socket
        within a minute, without waiting for a rebuild."""
        if self._desired_fn is None:
            return
        with self._lock:
            current = list(self._symbols)
        desired = self._current_desired()
        if desired != current:
            log.info("tick feed %s/%s: desired set drifted — resyncing "
                     "(%d → %d symbols)", self.user_id, self.broker,
                     len(current), len(desired))
            self.set_symbols(desired)

    # --- SDK callbacks (run on the SDK's threads) ----------------------------
    def _on_connect(self) -> None:
        self._record(state="connected", last_connect_at=None)
        log.info("tick feed %s/%s connected", self.user_id, self.broker)
        self._publish({"type": "feed", "state": "connected",
                       "ts": round(time.time(), 3)})
        self._sync_subscription()

    def _on_message(self, message) -> None:
        if not isinstance(message, dict):
            return
        ts_gateway = time.time()
        tick = _normalize_tick(message, self.broker, ts_gateway)
        if tick is None:
            return
        symbol = f"{tick['instrument']['exchange']}:{tick['instrument']['symbol']}"
        with self._lock:
            if symbol not in self._symbols:
                return  # a subscription this account no longer wants
            self._ticks_published += 1
            self._last_seen[symbol] = ts_gateway
            self._last_ticks[symbol] = tick
            self._last_tick_at = _now_iso()
            self._state = "connected"
        self._publish(tick)

    def _on_error(self, message) -> None:
        text = (message.get("message") if isinstance(message, dict)
                else str(message) or type(message).__name__)
        code = message.get("code") if isinstance(message, dict) else None
        self._record(error=str(text))
        if code in (-99, -300, 11001):  # expired / invalid token → rebuild fast
            log.warning("tick feed %s/%s: auth error (%s) — rebuilding",
                        self.user_id, self.broker, code)
            self._teardown()

    def _on_close(self, message) -> None:
        self._record(state="reconnecting",
                     error=str((message or {}).get("message", "")) or None)

    # --- bus -----------------------------------------------------------------
    def _publish(self, payload: dict) -> None:
        self._bus.publish(self.channel, payload)

    def _publish_heartbeat(self) -> None:
        with self._lock:
            ages = {s: round(time.time() - t, 1)
                    for s, t in self._last_seen.items()}
        self._publish({"type": "heartbeat", "ts": round(time.time(), 3),
                       "subscriptions": list(self._symbols), "ages": ages})

    def _record(self, *, state: Optional[str] = None,
                error: Optional[str] = None,
                last_connect_at: Optional[str] = "__unset__") -> None:
        with self._lock:
            if state is not None:
                self._state = state
            if error is not None:
                self._last_error = error
            if last_connect_at != "__unset__" and state == "connected":
                self._last_connect_at = _now_iso()


def _now_iso() -> str:
    from app.brokers.base import _now_iso as iso

    return iso()


#: The consumer id the dashboard's tick page claims symbols under. Strategy
#: processes claim under their ``strategy_id`` (from the runtime config).
DASHBOARD_CONSUMER = "dashboard"


class TickService:
    """Orchestrates the per-account feed workers (state in S3, threads in-process)."""

    def __init__(self, settings: Optional[Settings] = None,
                 store: Optional[BrokerStore] = None,
                 broker_service=None,
                 bus: Optional[TickBus] = None):
        self.settings = settings or get_settings()
        self.store = store or BrokerStore()
        self.broker_service = broker_service  # BrokerService (runtime_credentials)
        self.bus = bus or TickBus(self.settings)
        self._feeds: Dict[Tuple[str, str], FyersTickFeed] = {}
        self._lock = threading.Lock()

    # --- user-facing ---------------------------------------------------------
    def enable(self, user_id: str, broker: str) -> TickFeedStatus:
        self._require_feed(broker)
        doc = self._require_connected(user_id, broker)
        if doc.tick is None:
            doc.tick = TickFeedState()
        doc.tick.enabled = True
        doc.tick.enabled_at = doc.tick.enabled_at or _now_iso()
        doc.tick.updated_at = _now_iso()
        self.store.put(doc)
        self._ensure_worker(doc)
        return self.status(user_id, broker)

    def disable(self, user_id: str, broker: str) -> TickFeedStatus:
        self._require_feed(broker)
        doc = self.store.get(user_id, broker)
        if doc is not None and doc.tick is not None:
            doc.tick.enabled = False
            doc.tick.updated_at = _now_iso()
            self.store.put(doc)
        self._stop_worker(user_id, broker)
        return self.status(user_id, broker)

    def subscribe(self, user_id: str, broker: str, symbols: List[str],
                  consumer: str = DASHBOARD_CONSUMER) -> TickFeedStatus:
        """Claim ``symbols`` for ``consumer`` (the durable registry).

        Adds one claim per symbol per consumer (idempotent upsert — a
        re-ensure never duplicates), enforces the per-account cap over the
        DISTINCT claimed symbols, and the worker's ``set_symbols`` diff keeps
        the broker socket on the union. ``consumer`` is ``"dashboard"`` for
        the tick page, a strategy id for strategy processes."""
        self._require_feed(broker)
        normalized = [s for s in (_normalize_symbol(s) for s in symbols) if s]
        normalized = sorted(set(normalized))
        if not normalized:
            raise TickError("no symbols given")
        doc = self._require_connected(user_id, broker)
        state = doc.tick or TickFeedState()
        claims = dict(state.claims or {})
        for symbol in normalized:
            claims.setdefault(symbol, {})
            claims[symbol][consumer] = TickClaim(claimed_at=_now_iso())
        if len(claims) > self.settings.tick_max_symbols:
            raise TickError(
                f"too many subscriptions ({len(claims)}) — the per-account "
                f"cap is {self.settings.tick_max_symbols}")
        state.claims = claims
        state.updated_at = _now_iso()
        doc.tick = state
        self.store.put(doc)
        self._ensure_worker(doc)
        return self.status(user_id, broker)

    def unsubscribe(self, user_id: str, broker: str, symbols: List[str],
                    consumer: str = DASHBOARD_CONSUMER) -> TickFeedStatus:
        """Release ``consumer``'s claims on ``symbols``.

        Removes only that consumer's stake; a symbol stays subscribed for as
        long as ANY consumer holds it, and drops from the broker socket the
        moment the LAST claim goes (the worker's ``set_symbols`` also purges
        its last-tick cache for dropped symbols)."""
        self._require_feed(broker)
        normalized = [s for s in (_normalize_symbol(s) for s in symbols) if s]
        if not normalized:
            raise TickError("no symbols given")
        doc = self.store.get(user_id, broker)
        if doc is None or doc.tick is None:
            raise TickError(f"tick data is not enabled for {broker}")
        claims = dict(doc.tick.claims or {})
        for symbol in normalized:
            holders = dict(claims.get(symbol) or {})
            if consumer not in holders:
                continue  # this consumer never claimed it (or already let go)
            del holders[consumer]
            if holders:
                claims[symbol] = holders
            else:
                claims.pop(symbol, None)
        doc.tick.claims = claims
        doc.tick.updated_at = _now_iso()
        self.store.put(doc)
        feed = self._feed(user_id, broker)
        if feed is not None:
            feed.set_symbols(self._desired(doc))
        return self.status(user_id, broker)

    def status(self, user_id: str, broker: str) -> TickFeedStatus:
        doc = self.store.get(user_id, broker)
        tick = doc.tick if doc is not None else None
        feed = self._feed(user_id, broker)
        live = feed.snapshot() if feed is not None else {}
        # A worker that exited (thread dead) is "stopped".
        state = live.get("state")
        if feed is not None and not feed.is_alive():
            state = "stopped"
        return TickFeedStatus(
            broker=broker,
            enabled=bool(tick and tick.enabled),
            subscriptions=self._desired(doc) if tick else [],
            claims={symbol: sorted(holders)
                    for symbol, holders in sorted((tick.claims or {}).items())
                    } if tick else {},
            state=state if (tick and tick.enabled) else None,
            last_error=live.get("last_error"),
            ticks_published=live.get("ticks_published", 0),
            last_tick_at=live.get("last_tick_at"),
            last_connect_at=live.get("last_connect_at"),
        )

    # --- lifecycle -----------------------------------------------------------
    def reconcile_on_startup(self) -> None:
        """Re-arm enabled feeds from S3 (deliberately non-fatal)."""
        for doc in self.store.list_all():
            if doc.tick is None or not doc.tick.enabled:
                continue
            try:
                self._ensure_worker(doc)
            except Exception as exc:  # noqa: BLE001 — never fatal at startup
                log.warning("tick feed %s/%s not started: %s",
                            doc.user_id, doc.broker, exc)

    def shutdown(self) -> None:
        for feed in list(self._feeds.values()):
            feed.stop()
            self._teardown_join(feed)

    def stop_for_broker_disable(self, user_id: str, broker: str) -> None:
        """The broker connection was disabled — the feed cannot survive it."""
        self._stop_worker(user_id, broker)

    # --- the strategy contract ----------------------------------------------
    def handle_control_request(self, feed: FyersTickFeed, request: dict) -> dict:
        """Process one control-channel request from a strategy process (the
        ``quant_client`` lib) and return the ack payload, which the feed
        publishes on the data channel.

        Two v0 actions, both per-account and deliberate:
        - ``subscribe`` — add a CLAIM for ``consumer`` (idempotent; persists
          in S3);
        - ``unsubscribe`` — release ``consumer``'s claim on ``symbols``; a
          symbol drops from the broker socket only when its LAST claim goes,
          so two strategies sharing a symbol keep it alive for each other
          until both let go.
        - ``release-mine`` — release EVERY claim ``consumer`` holds across the
          account (a strategy's end-of-run sweep: it may not know which
          symbols it still has claims on, the registry does).
        ``consumer`` is the strategy's ``strategy_id`` (from the runtime
        config); an unidentified caller is attributed to ``unknown-strategy``
        (prunable from the dashboard). The ack carries the feed's last-tick
        snapshot so a strategy joining mid-session has current state.
        """
        request_id = request.get("request_id")
        action = request.get("action")
        consumer = str(request.get("consumer") or "unknown-strategy")
        try:
            doc = self.store.get(feed.user_id, feed.broker)
            if doc is None or doc.tick is None or not doc.tick.enabled:
                raise TickError(f"tick data is not enabled for {feed.broker}")
            if action == "release-mine":
                claims = dict(doc.tick.claims or {})
                removed: List[str] = []
                for symbol in list(claims):
                    holders = dict(claims[symbol])
                    if consumer in holders:
                        del holders[consumer]
                        removed.append(symbol)
                        if holders:
                            claims[symbol] = holders
                        else:
                            claims.pop(symbol, None)
                if removed:
                    doc.tick.claims = claims
                    doc.tick.updated_at = _now_iso()
                    self.store.put(doc)
                    feed.set_symbols(self._desired(doc))
                    log.info("tick claims released for %s on %s/%s: %s",
                             consumer, feed.user_id, feed.broker, removed)
                return {
                    "type": "ack",
                    "action": "release-mine",
                    "request_id": request_id,
                    "broker": feed.broker,
                    "removed": sorted(removed),
                    "subscriptions": self._desired(doc),
                    "state": None,
                    "error": None,
                    "snapshot": feed.snapshot_ticks(),
                    "ts": round(time.time(), 3),
                }
            symbols = request.get("symbols")
            if not isinstance(symbols, list) or not symbols:
                raise TickError("no symbols given")
            previous = set(self.subscriptions(feed.user_id, feed.broker))
            if action == "subscribe":
                status = self.subscribe(feed.user_id, feed.broker, symbols,
                                        consumer=consumer)
                return {
                    "type": "ack",
                    "action": "subscribe",
                    "request_id": request_id,
                    "broker": feed.broker,
                    "added": [s for s in status.subscriptions
                              if s not in previous],
                    "subscriptions": status.subscriptions,
                    "state": status.state,
                    "error": status.last_error,
                    "snapshot": feed.snapshot_ticks(),
                    "ts": round(time.time(), 3),
                }
            if action == "unsubscribe":
                status = self.unsubscribe(feed.user_id, feed.broker, symbols,
                                          consumer=consumer)
                return {
                    "type": "ack",
                    "action": "unsubscribe",
                    "request_id": request_id,
                    "broker": feed.broker,
                    "removed": sorted(previous - set(status.subscriptions)),
                    "subscriptions": status.subscriptions,
                    "state": status.state,
                    "error": status.last_error,
                    "snapshot": feed.snapshot_ticks(),
                    "ts": round(time.time(), 3),
                }
            raise TickError(
                f"unsupported control action {action!r} — v0 supports "
                f"'subscribe' and 'unsubscribe'")
        except TickError as exc:
            return {
                "type": "ack",
                "action": action,
                "request_id": request_id,
                "broker": feed.broker,
                "error": str(exc),
                "ts": round(time.time(), 3),
            }

    @staticmethod
    def _desired(doc: BrokerDocument) -> List[str]:
        """The union of claimed symbols — what the broker socket subscribes."""
        if doc is None or doc.tick is None:
            return []
        return sorted({symbol for symbol, holders
                       in (doc.tick.claims or {}).items() if holders})

    def subscriptions(self, user_id: str, broker: str) -> List[str]:
        """The current desired set (union of claims) from the S3 document —
        collected first: STRATEGY claims older than
        ``tick_claim_ttl_hours`` are released (a halted/crashed strategy
        process cannot unsubscribe for itself; the dashboard's claims are
        exempt — they are the operator's standing intent). The write happens
        only when something actually expired, and the live socket follows
        through the worker's drift check."""
        doc = self.store.get(user_id, broker)
        if doc is None or doc.tick is None:
            return []
        ttl_seconds = self.settings.tick_claim_ttl_hours * 3600.0
        now = time.time()
        claims = doc.tick.claims or {}
        expired: list = []
        for symbol in list(claims):
            holders = claims[symbol]
            for consumer in list(holders):
                if consumer == DASHBOARD_CONSUMER:
                    continue
                claimed_at = holders[consumer].claimed_at
                try:
                    claimed_ts = datetime.fromisoformat(claimed_at).timestamp()
                except (ValueError, TypeError):
                    continue  # an unparseable stamp never expires silently
                if now - claimed_ts > ttl_seconds:
                    expired.append(f"{symbol}/{consumer}")
                    del holders[consumer]
            if not holders:
                claims.pop(symbol, None)
        if expired:
            doc.tick.claims = claims
            doc.tick.updated_at = _now_iso()
            self.store.put(doc)
            log.info("tick claims expired for %s/%s: %s", user_id, broker,
                     ", ".join(expired))
            feed = self._feed(user_id, broker)
            if feed is not None:
                feed.set_symbols(self._desired(doc))
        return self._desired(doc)

    def release_strategy_claims(self, strategy_id: str) -> List[str]:
        """Release every claim held by ``strategy_id`` across all enabled
        feeds — wired into HALT (the platform killed the run, so its claims
        die with it; there is never a live run during the BTST overnight
        hold, so this conflicts with nothing). Dashboard claims are
        untouched, as are other strategies'. Best-effort and silent on
        store failures — the halt itself must still succeed."""
        released: List[str] = []
        try:
            for doc in self.store.list_all():
                if doc.tick is None or not doc.tick.enabled:
                    continue
                claims = dict(doc.tick.claims or {})
                touched = False
                for symbol in list(claims):
                    holders = dict(claims[symbol])
                    if strategy_id in holders:
                        del holders[strategy_id]
                        touched = True
                        released.append(symbol)
                        if holders:
                            claims[symbol] = holders
                        else:
                            claims.pop(symbol, None)
                if touched:
                    doc.tick.claims = claims
                    doc.tick.updated_at = _now_iso()
                    self.store.put(doc)
                    feed = self._feed(doc.user_id, doc.broker)
                    if feed is not None:
                        feed.set_symbols(self._desired(doc))
        except Exception as exc:  # noqa: BLE001 — a failed release is logged
            log.warning("could not release tick claims for %s: %s",
                        strategy_id, type(exc).__name__)
        return sorted(set(released))

    def data_section(self, user_id: Optional[str]) -> dict:
        """The ``data`` section of a run's runtime config.

        Mirrors ``runtime_credentials``: the runner calls this on every launch
        so a strategy discovers its account's tick gateway without any
        hard-coded channel — ``config["data"]["gateway"]["<broker>"]["channel"]``.
        Empty when no tick feed is enabled and, deliberately, on ANY storage
        failure: a broken store must stop the *data section*, never the run.
        """
        if not user_id:
            return {}
        gateway: dict = {}
        for broker in TICK_FEEDS:
            try:
                doc = self.store.get(user_id, broker)
                if doc is None or doc.tick is None or not doc.tick.enabled:
                    continue
                gateway[broker] = {
                    "transport": "redis-pubsub",
                    "url": self.settings.redis_url,
                    "channel": channel_for(user_id, broker),
                    "subscriptions": self._desired(doc),
                    "claims": {symbol: sorted(holders)
                               for symbol, holders
                               in (doc.tick.claims or {}).items()},
                }
            except Exception as exc:  # noqa: BLE001 — skip this broker only
                log.warning("tick gateway info unavailable for %s/%s: %s",
                            user_id, broker, type(exc).__name__)
                continue
        return {"gateway": gateway} if gateway else {}

    # --- workers -------------------------------------------------------------
    def _ensure_worker(self, doc: BrokerDocument) -> None:
        if doc.tick is None or not doc.tick.enabled:
            return
        if doc.status != "connected":
            raise TickError(
                f"{doc.broker} must be connected before enabling tick data")
        key = (doc.user_id, doc.broker)
        with self._lock:
            feed = self._feeds.get(key)
            if feed is not None and feed.is_alive():
                feed.set_symbols(self._desired(doc))
                return
            feed = self._make_feed(doc)
            self._feeds[key] = feed
        feed.start()
        log.info("tick feed started for %s/%s (%d symbols)",
                 doc.user_id, doc.broker, len(self._desired(doc)))

    def _make_feed(self, doc: BrokerDocument) -> FyersTickFeed:
        def credentials() -> Optional[dict]:
            section = (self.broker_service.runtime_credentials(doc.user_id)
                       if self.broker_service is not None else {})
            return section.get(doc.broker)

        def desired() -> List[str]:
            # Fresh read of the UNION of claims — the live socket must project
            # the source of truth even after behind-the-back doc changes.
            return self.subscriptions(doc.user_id, doc.broker)

        feed = FyersTickFeed(doc.user_id, doc.broker, credentials, self.bus,
                             desired_fn=desired)
        feed.set_symbols(self._desired(doc))
        # The feed's control thread routes requests through the service, which
        # owns the S3 document and the per-account cap.
        feed._control_handler = (
            lambda request: self.handle_control_request(feed, request))
        return feed

    def _feed(self, user_id: str, broker: str) -> Optional[FyersTickFeed]:
        with self._lock:
            return self._feeds.get((user_id, broker))

    def _stop_worker(self, user_id: str, broker: str) -> None:
        with self._lock:
            feed = self._feeds.pop((user_id, broker), None)
        if feed is not None:
            feed.stop()
            self._teardown_join(feed)
            log.info("tick feed stopped for %s/%s", user_id, broker)

    @staticmethod
    def _teardown_join(feed: FyersTickFeed) -> None:
        if feed.is_alive():
            feed.join(timeout=10)

    # --- guards --------------------------------------------------------------
    @staticmethod
    def _require_feed(broker: str) -> None:
        if broker not in TICK_FEEDS:
            raise TickError(
                f"tick data is not available for {broker} yet "
                f"(supported: {', '.join(sorted(TICK_FEEDS))})")

    def _require_connected(self, user_id: str, broker: str) -> BrokerDocument:
        doc = self.store.get(user_id, broker)
        if doc is None:
            raise TickError(f"{broker} is not connected for this account")
        if doc.status != "connected":
            raise TickError(
                f"{broker} must be connected before enabling tick data")
        return doc
