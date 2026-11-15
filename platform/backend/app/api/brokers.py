"""Broker connection endpoints (per user).

The caller reaches only their own connections — the routes operate on the
authenticated principal's user id, so no cross-user access exists by
construction. Credentials are accepted once on enable and never echoed:
responses carry only non-secret status metadata.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_broker_service
from app.core.security import Principal, get_current_user
from app.models.broker import (
    BrokerListResponse,
    DhanEnableRequest,
)
from app.services.broker_service import BrokerError, BrokerService

router = APIRouter(prefix="/brokers", tags=["brokers"],
                   dependencies=[Depends(get_current_user)])


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


@router.post("/dhan/enable", response_model=dict,
             status_code=status.HTTP_202_ACCEPTED)
def enable_dhan(body: DhanEnableRequest,
                principal: Principal = Depends(get_current_user),
                svc: BrokerService = Depends(get_broker_service)) -> dict:
    """Enable Dhan for the caller.

    Returns **202** with ``status=connecting`` — the backend keeps trying for
    up to a few TOTP cycles (~90 s) and the dashboard polls ``GET /brokers``
    for the outcome. Credentials are stored in Parameter Store (SSM) and never
    echoed back.
    """
    try:
        doc = svc.enable(_user_principal(principal), body)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"broker": doc.broker, "status": doc.status}


@router.post("/dhan/retry", response_model=dict)
def retry_dhan(principal: Principal = Depends(get_current_user),
               svc: BrokerService = Depends(get_broker_service)) -> dict:
    """Retry a failed connection with the already-stored credentials."""
    try:
        doc = svc.retry(_user_principal(principal))
    except BrokerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"broker": doc.broker, "status": doc.status}


@router.post("/dhan/disable", response_model=dict)
def disable_dhan(principal: Principal = Depends(get_current_user),
                 svc: BrokerService = Depends(get_broker_service)) -> dict:
    """Disconnect Dhan: credentials, token, schedule and record are removed."""
    try:
        svc.disable(_user_principal(principal))
    except BrokerError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"disabled": True}
