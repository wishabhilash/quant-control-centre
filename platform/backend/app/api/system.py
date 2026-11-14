"""System health endpoints (spec section 14)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app import __version__
from app.api.deps import get_log_service, get_process_service, get_scheduler_service, get_store
from app.core.config import Settings, get_settings
from app.core.security import require_auth
from app.models.system import HealthResponse, SchedulerStatus, StorageStatus
from app.services.log_service import LogService
from app.services.process_service import ProcessService
from app.services.scheduler_service import SchedulerService
from app.storage.strategy_store import StrategyStore

router = APIRouter(prefix="/system", tags=["system"], dependencies=[Depends(require_auth)])


@router.get("/health", response_model=HealthResponse)
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(status="ok", environment=settings.environment, version=__version__,
                          checks={"s3": "configured", "scheduler": "ok"})


@router.get("/storage", response_model=StorageStatus)
def storage(store: StrategyStore = Depends(get_store),
            settings: Settings = Depends(get_settings)) -> StorageStatus:
    reachable = store.storage.check_connectivity()
    return StorageStatus(reachable=reachable, bucket=settings.s3_bucket, region=settings.region,
                        endpoint_url=settings.s3_endpoint_url,
                        detail="ok" if reachable else "unreachable")


@router.get("/scheduler/preview")
def scheduler_preview(cron: str, timezone: str | None = None, count: int = 5,
                      svc: SchedulerService = Depends(get_scheduler_service)) -> dict:
    """Preview the upcoming runs of a cron expression (before it is saved).

    Like AWS EventBridge's "next occurrences" list: the editor shows what a
    cron expression actually means (including the day-of-week surprises —
    ``1-5`` is Monday–Friday, so it never fires on a weekend).
    """
    try:
        runs = svc.upcoming(cron, timezone, count)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return {"cron": cron, "timezone": timezone or svc.settings.default_timezone,
            "runs": runs}


@router.get("/scheduler", response_model=SchedulerStatus)
def scheduler(svc: SchedulerService = Depends(get_scheduler_service),
              settings: Settings = Depends(get_settings)) -> SchedulerStatus:
    enabled = svc.collect_enabled_schedules()
    runner_python, runner_script = svc.runner_paths()
    # Explain the state of the crontab: the platform writes the crontab of the
    # account it runs as (the service account), not root's.
    last = svc.last_reconcile or {}
    if settings.scheduler_dry_run:
        detail = "dry-run (no system crontab)"
    elif last and not last.get("installed", False):
        detail = f"cron install failed: {last.get('reason', 'unknown reason')}"
    elif last:
        detail = (f"installed ({last.get('entries', 0)} entries) in the crontab of "
                  f"the platform account")
    else:
        detail = "system_cron (no reconcile yet)"
    return SchedulerStatus(active=not settings.scheduler_dry_run,
                           mode="dry_run" if settings.scheduler_dry_run else "system_cron",
                           managed_schedule_count=len(enabled),
                           cron_file=str(settings.platform_cron_file_path()),
                           runner_python=runner_python,
                           runner_script=runner_script,
                           strategies_dir=str(settings.strategies_dir_path()),
                           detail=detail)
