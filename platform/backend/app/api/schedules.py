"""Schedule management endpoints (spec section 14).

Schedules belong to the strategy, and the strategy belongs to a user — so every
route enforces the strategy's ownership.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_scheduler_service, get_strategy_service
from app.core.security import Principal, get_current_user, require_auth
from app.models.strategy import Schedule, ScheduleCreate, ScheduleUpdate
from app.services.scheduler_service import SchedulerService
from app.services.strategy_service import StrategyService, StrategyServiceError

router = APIRouter(prefix="/strategies", tags=["schedules"], dependencies=[Depends(require_auth)])


def _require_owner(strategy_id: str, principal: Principal,
                   svc: StrategyService) -> None:
    try:
        svc.get_for_user(strategy_id, principal)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN if "access" in str(exc)
                            else status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/{strategy_id}/schedules")
def list_schedules(strategy_id: str, principal: Principal = Depends(get_current_user),
                    svc: SchedulerService = Depends(get_scheduler_service),
                    strategies: StrategyService = Depends(get_strategy_service)) -> dict:
    _require_owner(strategy_id, principal, strategies)
    try:
        doc = svc.strategy_service.get(strategy_id)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    return {"strategy_id": strategy_id, "schedules": [s.model_dump() for s in doc.schedules]}


@router.post("/{strategy_id}/schedules", response_model=Schedule, status_code=status.HTTP_201_CREATED)
def create_schedule(strategy_id: str, req: ScheduleCreate,
                    principal: Principal = Depends(get_current_user),
                    svc: SchedulerService = Depends(get_scheduler_service),
                    strategies: StrategyService = Depends(get_strategy_service)) -> Schedule:
    _require_owner(strategy_id, principal, strategies)
    try:
        sched = svc.create_schedule(strategy_id, req.name, req.cron_expression, req.timezone, req.enabled, req.command)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    svc.reconcile(strategy_id)
    return sched


@router.put("/{strategy_id}/schedules/{schedule_id}", response_model=Schedule)
def update_schedule(strategy_id: str, schedule_id: str, patch: ScheduleUpdate,
                    principal: Principal = Depends(get_current_user),
                    svc: SchedulerService = Depends(get_scheduler_service),
                    strategies: StrategyService = Depends(get_strategy_service)) -> Schedule:
    _require_owner(strategy_id, principal, strategies)
    try:
        sched = svc.update_schedule(strategy_id, schedule_id, patch)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    svc.reconcile(strategy_id)
    return sched


@router.delete("/{strategy_id}/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(strategy_id: str, schedule_id: str,
                    principal: Principal = Depends(get_current_user),
                    svc: SchedulerService = Depends(get_scheduler_service),
                    strategies: StrategyService = Depends(get_strategy_service)):
    _require_owner(strategy_id, principal, strategies)
    try:
        svc.delete_schedule(strategy_id, schedule_id)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    svc.reconcile(strategy_id)


@router.post("/{strategy_id}/schedules/{schedule_id}/enable", response_model=Schedule)
def enable_schedule(strategy_id: str, schedule_id: str,
                    principal: Principal = Depends(get_current_user),
                    svc: SchedulerService = Depends(get_scheduler_service),
                    strategies: StrategyService = Depends(get_strategy_service)) -> Schedule:
    _require_owner(strategy_id, principal, strategies)
    try:
        doc = svc.enable_schedule(strategy_id, schedule_id)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    svc.reconcile(strategy_id)
    return next(s for s in doc.schedules if s.schedule_id == schedule_id)


@router.post("/{strategy_id}/schedules/{schedule_id}/disable")
def disable_schedule(strategy_id: str, schedule_id: str,
                     principal: Principal = Depends(get_current_user),
                     svc: SchedulerService = Depends(get_scheduler_service),
                     strategies: StrategyService = Depends(get_strategy_service)) -> dict:
    _require_owner(strategy_id, principal, strategies)
    try:
        svc.disable_schedule(strategy_id, schedule_id)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    svc.reconcile(strategy_id)
    return {"strategy_id": strategy_id, "schedule_id": schedule_id, "enabled": False}
