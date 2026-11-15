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
    sched = svc.strategy_scheduler
    active = sched.enabled and not settings.scheduler_dry_run
    scheduled = svc.scheduled_overview()
    # Explain the scheduler state: dry-run keeps schedules stored in S3 but
    # fires nothing; when active, the last reconcile outcome is summarised.
    if not active:
        detail = "dry-run (schedules stay in S3; the in-process scheduler is not running)"
    else:
        last = svc.last_reconcile or {}
        detail = f"APScheduler running — {len(scheduled)} job(s) scheduled"
        skipped = last.get("skipped") or []
        if skipped:
            detail += (f"; {len(skipped)} schedule(s) skipped: invalid cron "
                       f"({', '.join(s.get('schedule_id', '?') for s in skipped)})")
        elif last:
            detail += (f"; last reconcile: {last.get('added', 0)} added, "
                       f"{last.get('removed', 0)} removed")
    return SchedulerStatus(active=active,
                           mode="apscheduler" if active else "dry_run",
                           managed_schedule_count=len(enabled),
                           scheduled_jobs=len(scheduled),
                           job_store=str(sched.job_store_path) if active else None,
                           runner_python=runner_python,
                           runner_script=runner_script,
                           strategies_dir=str(settings.strategies_dir_path()),
                           detail=detail)
