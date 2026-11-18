"""Broker connection endpoints (per user, per broker).

The caller reaches only their own connections — the routes operate on the
authenticated principal's user id, so no cross-user access exists by
construction. Credentials are accepted once on enable and never echoed:
responses carry only non-secret status metadata.

The routes are parameterised by broker (``/brokers/dhan/enable``,
``/brokers/fyers/enable`` …) and dispatch to the right request model, so
adding a broker never grows the route table.
"""
from __future__ import annotations

import time

import pydantic
from fastapi import (APIRouter, Depends, HTTPException, WebSocket,
                     WebSocketDisconnect, status)

from app.api.deps import get_broker_service, get_tick_service
from app.brokers import ADAPTERS
from app.core.security import Principal, get_current_user
from app.models.broker import (
    BrokerListResponse,
    BrokerName,
    TickFeedStatus,
    TickSubscribeRequest,
)
from app.services.broker_service import BrokerError, BrokerService
from app.services.tick_service import TICK_FEEDS, TickError, TickService

router = APIRouter(prefix="/brokers", tags=["brokers"],
                   dependencies=[Depends(get_current_user)])

# The request model each broker's *enable* form maps to — derived from the
# adapter registry, so adding a broker never grows this route table.
_ENABLE_MODELS = {name: a.enable_model for name, a in ADAPTERS.items()}


def _user_principal(principal: Principal) -> str:
    """The caller's user id — brokers belong to a *user account*.

    The operator principal (break-glass token, or an auth-disabled request
    without a token) has no user account to attach a connection to; letting it
    enable a broker would store credentials under ``admin``, where no strategy
    (every strategy is owned by a user) would ever see the token. The operator
    must sign in as the user instead — an explicit, audited action.
    """
    if principal.is_admin:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "brokers belong to a user account — sign in as the user "
            "(operator sessions cannot connect a broker)",
        )
    return principal.user_id


@router.get("", response_model=BrokerListResponse)
def list_brokers(principal: Principal = Depends(get_current_user),
                 svc: BrokerService = Depends(get_broker_service)) -> BrokerListResponse:
    """The caller's broker connections and their status."""
    return svc.list_for_user(_user_principal(principal))


@router.post("/{broker}/enable", response_model=dict,
             status_code=status.HTTP_202_ACCEPTED)
def enable_broker(broker: BrokerName,
                  body: dict,
                  principal: Principal = Depends(get_current_user),
                  svc: BrokerService = Depends(get_broker_service)) -> dict:
    """Enable a broker for the caller.

    Returns **202** with ``status=connecting`` — the backend keeps trying for
    up to a few TOTP cycles (~90 s) and the dashboard polls ``GET /brokers``
    for the outcome. Credentials are stored in Parameter Store (SSM) and
    never echoed back.
    """
    _user_principal(principal)
    model = _ENABLE_MODELS.get(broker)
    if model is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unsupported broker: {broker}")
    try:
        req = model.model_validate(body)
    except pydantic.ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"invalid {broker} credentials: "
                            f"{exc.errors()[0].get('msg', 'validation failed')}") from exc
    try:
        doc = svc.enable(_user_principal(principal), broker, req)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"broker": doc.broker, "status": doc.status}


@router.post("/{broker}/retry", response_model=dict)
def retry_broker(broker: BrokerName,
                 principal: Principal = Depends(get_current_user),
                 svc: BrokerService = Depends(get_broker_service)) -> dict:
    """Retry a failed connection with the already-stored credentials."""
    _user_principal(principal)
    try:
        doc = svc.retry(_user_principal(principal), broker)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"broker": doc.broker, "status": doc.status}


@router.post("/{broker}/rotate", response_model=dict,
             status_code=status.HTTP_202_ACCEPTED)
def rotate_broker(broker: BrokerName,
                  principal: Principal = Depends(get_current_user),
                  svc: BrokerService = Depends(get_broker_service)) -> dict:
    """Force a token rotation NOW (the 08:00 job, on demand).

    Returns **202** with ``status=rotating`` — the dashboard polls
    ``GET /brokers`` until ``last_token_at`` advances (success) or
    ``last_error`` appears (failure; the connection survives, since the
    previous token stays stored until the broker expires it). The tick feed
    picks the new token up within its credentials-cache TTL and rebuilds its
    socket on its own.
    """
    _user_principal(principal)
    try:
        doc = svc.rotate(_user_principal(principal), broker)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"broker": doc.broker, "status": "rotating"}


@router.post("/{broker}/disable", response_model=dict)
def disable_broker(broker: BrokerName,
                   principal: Principal = Depends(get_current_user),
                   svc: BrokerService = Depends(get_broker_service),
                   ticks: TickService = Depends(get_tick_service)) -> dict:
    """Disconnect a broker: credentials, token, schedule and record are removed."""
    user_id = _user_principal(principal)
    try:
        svc.disable(user_id, broker)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    # The tick feed cannot survive its broker connection — stop it too.
    try:
        ticks.stop_for_broker_disable(user_id, broker)
    except Exception:  # noqa: BLE001 — the disable must not fail on the feed
        pass
    return {"disabled": True}


# --- Tick data (market-data feed, per account) -------------------------------
# The routes are broker-parameterised; the per-broker gateway registry
# (TICK_FEEDS) decides which brokers have a feed at all — Dhan lands later
# without growing this table. The live stream reaches the UI over a WebSocket
# that forwards the account's Redis channel (transient bus, nothing stored).


def _tick_guards(broker: BrokerName, principal: Principal,
                 ticks: TickService) -> str:
    user_id = _user_principal(principal)
    if broker not in TICK_FEEDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"tick data is not available for {broker} yet "
            f"(supported: {', '.join(sorted(TICK_FEEDS))})")
    return user_id


@router.get("/{broker}/ticks/status", response_model=TickFeedStatus)
def tick_status(broker: BrokerName,
                principal: Principal = Depends(get_current_user),
                ticks: TickService = Depends(get_tick_service)) -> TickFeedStatus:
    """The caller's tick-feed status: enabled, live state, subscriptions."""
    user_id = _user_principal(principal)
    return ticks.status(user_id, broker)


@router.post("/{broker}/ticks/enable", response_model=TickFeedStatus)
def tick_enable(broker: BrokerName,
                principal: Principal = Depends(get_current_user),
                ticks: TickService = Depends(get_tick_service)) -> TickFeedStatus:
    """Start the account's tick feed (the broker must be connected)."""
    user_id = _tick_guards(broker, principal, ticks)
    try:
        return ticks.enable(user_id, broker)
    except TickError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/{broker}/ticks/disable", response_model=TickFeedStatus)
def tick_disable(broker: BrokerName,
                 principal: Principal = Depends(get_current_user),
                 ticks: TickService = Depends(get_tick_service)) -> TickFeedStatus:
    """Stop the account's tick feed (subscriptions are kept in the document)."""
    user_id = _tick_guards(broker, principal, ticks)
    try:
        return ticks.disable(user_id, broker)
    except TickError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/{broker}/ticks/subscribe", response_model=TickFeedStatus)
def tick_subscribe(broker: BrokerName,
                   body: TickSubscribeRequest,
                   principal: Principal = Depends(get_current_user),
                   ticks: TickService = Depends(get_tick_service)) -> TickFeedStatus:
    """Subscribe symbols (Fyers notation, e.g. ``NSE:SBIN-EQ``) on the feed."""
    user_id = _tick_guards(broker, principal, ticks)
    try:
        return ticks.subscribe(user_id, broker, body.symbols)
    except TickError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/{broker}/ticks/unsubscribe", response_model=TickFeedStatus)
def tick_unsubscribe(broker: BrokerName,
                     body: TickSubscribeRequest,
                     principal: Principal = Depends(get_current_user),
                     ticks: TickService = Depends(get_tick_service)) -> TickFeedStatus:
    """Remove symbols from the feed's subscription set."""
    user_id = _tick_guards(broker, principal, ticks)
    try:
        return ticks.unsubscribe(user_id, broker, body.symbols)
    except TickError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.websocket("/{broker}/ticks/stream")
async def tick_stream(broker: BrokerName, websocket: WebSocket,
                      ticks: TickService = Depends(get_tick_service)):
    """Live tick stream for the caller's own account (the logs-stream pattern:
    the token rides the query string, browsers cannot set headers on a
    handshake). Forwards the account's Redis pub/sub channel verbatim — the
    gateway is the producer, this socket is just a fan-out to the browser."""
    from app.api.deps import get_user_store
    from app.core.config import get_settings
    from app.core.security import _principal_from_token

    token = websocket.query_params.get("token", "")
    principal = None
    try:
        principal = _principal_from_token(token, get_settings(),
                                          get_user_store())
        if principal is not None and principal.is_admin:
            principal = None  # brokers belong to a user account (see enable)
    except Exception:  # noqa: BLE001
        principal = None
    if principal is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    channel = f"ticks:{principal.user_id}:{broker}"
    import asyncio

    async def _heartbeat() -> None:
        """Liveness keepalive so the UI can tell a quiet market from a dead
        socket — independent of message flow."""
        try:
            while True:
                await asyncio.sleep(15)
                await websocket.send_json({"type": "heartbeat",
                                           "ts": time.time()})
        except Exception:  # noqa: BLE001 — the socket is closing; fine
            return

    heartbeats = asyncio.create_task(_heartbeat())
    try:
        loop = asyncio.get_running_loop()
        sentinel = object()
        while True:
            try:
                # The bus generator blocks on Redis (~1 s poll); run it on an
                # executor thread so the event loop stays free.
                gen = ticks.bus.messages(channel)
                while True:
                    message = await loop.run_in_executor(None, next, gen,
                                                         sentinel)
                    if message is sentinel:
                        break  # generator ended (Redis dropped us) — reconnect
                    await websocket.send_json(message)
            except WebSocketDisconnect:
                raise
            except Exception as exc:  # noqa: BLE001 — Redis hiccup: surface it
                try:
                    await websocket.send_json(
                        {"type": "error",
                         "message": f"bus unavailable ({type(exc).__name__})"})
                except Exception:  # noqa: BLE001
                    return
                await asyncio.sleep(3)
    except WebSocketDisconnect:
        return
    finally:
        heartbeats.cancel()
