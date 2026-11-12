"""Deployment and cron-control endpoints (spec section 14).

Strategy start/stop are *cron* operations: "start" enables a strategy's schedules
(its crons are installed so the runner can trigger runs) and "stop" disables them.
The platform never launches or kills a strategy process directly -- only the
generic runner, invoked by cron, does (spec 10, 11).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_deploy_service, get_process_service, get_scheduler_service
from app.core.security import require_auth
from app.models.system import RuntimeStatus
from app.services.deploy_service import DeployService, DeployError
from app.services.process_service import ProcessService
from app.services.scheduler_service import SchedulerService

router = APIRouter(prefix="/strategies", tags=["deployment"], dependencies=[Depends(require_auth)])


@router.post("/{strategy_id}/deploy")
def deploy(strategy_id: str, wait_if_running: bool = False,
           svc: DeployService = Depends(get_deploy_service)) -> dict:
    try:
        return svc.deploy(strategy_id, wait_if_running=wait_if_running)
    except DeployError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/{strategy_id}/status", response_model=RuntimeStatus)
def runtime_status(strategy_id: str, svc: ProcessService = Depends(get_process_service)) -> RuntimeStatus:
    return svc.runtime_status(strategy_id)


@router.post("/{strategy_id}/start")
def start(strategy_id: str, svc: SchedulerService = Depends(get_scheduler_service)) -> dict:
    """Start a strategy = enable its cron schedules (crons are installed)."""
    return svc.enable_strategy(strategy_id)


@router.post("/{strategy_id}/stop")
def stop(strategy_id: str, svc: SchedulerService = Depends(get_scheduler_service)) -> dict:
    """Stop a strategy = disable its cron schedules so no further runs trigger."""
    return svc.disable_strategy(strategy_id)


@router.post("/{strategy_id}/restart")
def restart(strategy_id: str, svc: SchedulerService = Depends(get_scheduler_service)) -> dict:
    """Re-sync a strategy's crons (disable then enable)."""
    svc.disable_strategy(strategy_id)
    return svc.enable_strategy(strategy_id)
