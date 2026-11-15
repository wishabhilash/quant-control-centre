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

import pydantic
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_broker_service
from app.core.security import Principal, get_current_user
from app.models.broker import (
    BrokerListResponse,
    BrokerName,
    DhanEnableRequest,
    FyersEnableRequest,
)
from app.services.broker_service import BrokerError, BrokerService

router = APIRouter(prefix="/brokers", tags=["brokers"],
                   dependencies=[Depends(get_current_user)])

# The request model each broker's *enable* form maps to.
_ENABLE_MODELS = {"dhan": DhanEnableRequest, "fyers": FyersEnableRequest}


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


@router.post("/{broker}/disable", response_model=dict)
def disable_broker(broker: BrokerName,
                   principal: Principal = Depends(get_current_user),
                   svc: BrokerService = Depends(get_broker_service)) -> dict:
    """Disconnect a broker: credentials, token, schedule and record are removed."""
    _user_principal(principal)
    try:
        svc.disable(_user_principal(principal), broker)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"disabled": True}
