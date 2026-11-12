"""Strategy CRUD endpoints (spec section 14)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_strategy_service
from app.core.security import require_auth
from app.models.strategy import (
    StrategyCreate,
    StrategyDocument,
    StrategyListResponse,
    StrategyUpdate,
)
from app.services.strategy_service import StrategyService, StrategyServiceError

router = APIRouter(prefix="/strategies", tags=["strategies"], dependencies=[Depends(require_auth)])


@router.get("", response_model=StrategyListResponse)
def list_strategies(svc: StrategyService = Depends(get_strategy_service)) -> StrategyListResponse:
    return svc.list()


@router.post("", response_model=StrategyDocument, status_code=status.HTTP_201_CREATED)
def create_strategy(req: StrategyCreate, svc: StrategyService = Depends(get_strategy_service)) -> StrategyDocument:
    return svc.create(req)


@router.get("/{strategy_id}", response_model=StrategyDocument)
def get_strategy(strategy_id: str, svc: StrategyService = Depends(get_strategy_service)) -> StrategyDocument:
    try:
        return svc.get(strategy_id)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.put("/{strategy_id}", response_model=StrategyDocument)
def update_strategy(strategy_id: str, patch: StrategyUpdate,
                   svc: StrategyService = Depends(get_strategy_service)) -> StrategyDocument:
    try:
        return svc.update(strategy_id, patch)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_strategy(strategy_id: str, force_stop: bool = False,
                   svc: StrategyService = Depends(get_strategy_service)):
    try:
        result = svc.delete(strategy_id, force_stop=force_stop)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"deleted": True, **result}
