"""Strategy CRUD endpoints (spec section 14).

Every strategy belongs to the user who created it: the list is filtered by the
caller and every single-strategy route enforces ownership (the admin token
bypasses it).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.deps import get_copy_service, get_strategy_service
from app.core.security import Principal, get_current_user, require_auth
from app.models.strategy import (
    StrategyCreate,
    StrategyDocument,
    StrategyListResponse,
    StrategyUpdate,
)
from typing import TYPE_CHECKING

from app.services.strategy_service import StrategyService, StrategyServiceError

if TYPE_CHECKING:
    from app.services.copy_service import CopyService

router = APIRouter(prefix="/strategies", tags=["strategies"], dependencies=[Depends(require_auth)])


@router.get("", response_model=StrategyListResponse)
def list_strategies(page: int = 1, page_size: int = 10,
                    principal: Principal = Depends(get_current_user),
                    svc: StrategyService = Depends(get_strategy_service)) -> StrategyListResponse:
    return svc.list(principal, page=page, page_size=page_size)


@router.post("/{strategy_id}/copy-trading")
def set_copy_trading(strategy_id: str, body: dict,
                     principal: Principal = Depends(get_current_user),
                     svc: StrategyService = Depends(get_strategy_service),
                     copy: "CopyService" = Depends(get_copy_service)) -> dict:
    """The LEADER opts a strategy in (or out) of the copy-trading Universe.

    Owner-only. A copied strategy cannot itself be offered (no chains)."""
    from app.services.copy_service import CopyError

    try:
        doc = svc.get_for_user(strategy_id, principal)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN if "access" in str(exc)
                            else status.HTTP_404_NOT_FOUND, str(exc)) from exc
    try:
        return copy.set_copy_trading(strategy_id, bool(body.get("enabled")))
    except CopyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/{strategy_id}/follow", status_code=status.HTTP_201_CREATED)
def follow_strategy(strategy_id: str,
                    principal: Principal = Depends(get_current_user),
                    copy: "CopyService" = Depends(get_copy_service)) -> dict:
    """Follow a strategy = the platform creates YOUR OWN copy of it.

    The compliant model (run the algo for the user, under their control):
    a deep copy of the leader's project — disabled, with the leader's
    schedules as DISABLED drafts — owned by you, with YOUR broker tokens
    injected at run time, YOUR tick claims and YOUR logs. The leader's
    source code is never shown to you. Control: deploy, schedules, halt,
    kill switch — like any strategy of your own."""
    try:
        return copy.follow(strategy_id, principal.user_id)
    except CopyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("", response_model=StrategyDocument, status_code=status.HTTP_201_CREATED)
def create_strategy(req: StrategyCreate, principal: Principal = Depends(get_current_user),
                    svc: StrategyService = Depends(get_strategy_service)) -> StrategyDocument:
    return svc.create(req, owner_id=None if principal.is_admin else principal.user_id)


@router.get("/{strategy_id}", response_model=StrategyDocument)
def get_strategy(strategy_id: str, response: Response,
                 principal: Principal = Depends(get_current_user),
                 svc: StrategyService = Depends(get_strategy_service)) -> StrategyDocument:
    try:
        doc = svc.get_for_user(strategy_id, principal)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN if "access" in str(exc)
                            else status.HTTP_404_NOT_FOUND, str(exc)) from exc
    # Expose the S3 ETag as a transport header so clients can use conditional
    # (If-Match) writes to detect concurrent edits (spec 8). It is not stored
    # in the document, so the S3 JSON schema is untouched (spec 1, 5).
    etag = svc.store.get_etag(strategy_id)
    if etag:
        response.headers["ETag"] = etag
    return doc


@router.put("/{strategy_id}", response_model=StrategyDocument)
def update_strategy(strategy_id: str, patch: StrategyUpdate,
                   principal: Principal = Depends(get_current_user),
                   svc: StrategyService = Depends(get_strategy_service),
                   copy: "CopyService" = Depends(get_copy_service)) -> StrategyDocument:
    try:
        svc.get_for_user(strategy_id, principal)  # ownership check
        doc = svc.update(strategy_id, patch)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN if "access" in str(exc)
                            else status.HTTP_404_NOT_FOUND, str(exc)) from exc
    # Marketplace: keep the offering's name/description current (best-effort
    # — the hourly rebuild is the backstop for cosmetic drift).
    try:
        copy.sync_entry(doc)
    except Exception:  # noqa: BLE001
        pass
    return doc


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_strategy(strategy_id: str, force_stop: bool = False,
                    principal: Principal = Depends(get_current_user),
                    svc: StrategyService = Depends(get_strategy_service),
                    copy: "CopyService" = Depends(get_copy_service)):
    try:
        svc.get_for_user(strategy_id, principal)  # ownership check
        result = svc.delete(strategy_id, force_stop=force_stop)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    # Marketplace cleanup: the deleted strategy's offering and any follower
    # copies of it leave the index (best-effort — the hourly rebuild is the
    # backstop).
    try:
        copy.on_strategy_deleted(strategy_id)
    except Exception:  # noqa: BLE001
        pass
    return {"deleted": True, **result}
