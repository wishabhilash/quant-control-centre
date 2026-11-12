"""Schedule management endpoints (spec section 14)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_scheduler_service
from app.core.security import require_auth
from app.models.strategy import Schedule, ScheduleCreate, ScheduleUpdate
from app.services.scheduler_service import SchedulerService

router = APIRouter(prefix="/strategies", tags=["schedules"], dependencies=[Depends(require_auth)])


def _not_found(exc: Exception) -> HTTPException:
    if "not found" in str(exc).lower():
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/{strategy_id}/schedules")
def list_schedules(strategy_id: str, svc: SchedulerService = Depends(get_scheduler_service)) -> dict:
    try:
        doc = svc.strategy_service.get(strategy_id)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    return {"strategy_id": strategy_id, "schedules": [s.model_dump() for s in doc.schedules]}


@router.post("/{strategy_id}/schedules", response_model=Schedule, status_code=status.HTTP_201_CREATED)
def create_schedule(strategy_id: str, req: ScheduleCreate,
                   svc: SchedulerService = Depends(get_scheduler_service)) -> Schedule:
    try:
        sched = svc.create_schedule(strategy_id, req.name, req.cron_expression, req.timezone, req.enabled, req.command)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    svc.reconcile(strategy_id)
    return sched


@router.put("/{strategy_id}/schedules/{schedule_id}", response_model=Schedule)
def update_schedule(strategy_id: str, schedule_id: str, patch: ScheduleUpdate,
                   svc: SchedulerService = Depends(get_scheduler_service)) -> Schedule:
    try:
        sched = svc.update_schedule(strategy_id, schedule_id, patch)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    svc.reconcile(strategy_id)
    return sched


@router.delete("/{strategy_id}/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(strategy_id: str, schedule_id: str,
                   svc: SchedulerService = Depends(get_scheduler_service)):
    try:
        svc.delete_schedule(strategy_id, schedule_id)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    svc.reconcile(strategy_id)


@router.post("/{strategy_id}/schedules/{schedule_id}/enable", response_model=Schedule)
def enable_schedule(strategy_id: str, schedule_id: str,
                   svc: SchedulerService = Depends(get_scheduler_service)) -> Schedule:
    try:
        doc = svc.enable_schedule(strategy_id, schedule_id)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    svc.reconcile(strategy_id)
    return next(s for s in doc.schedules if s.schedule_id == schedule_id)


@router.post("/{strategy_id}/schedules/{schedule_id}/disable")
def disable_schedule(strategy_id: str, schedule_id: str,
                   svc: SchedulerService = Depends(get_scheduler_service)) -> dict:
    try:
        svc.disable_schedule(strategy_id, schedule_id)
    except Exception as exc:  # noqa: BLE001
        raise _not_found(exc)
    svc.reconcile(strategy_id)
    return {"strategy_id": strategy_id, "schedule_id": schedule_id, "enabled": False}
