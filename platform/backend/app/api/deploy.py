"""Deployment and cron-control endpoints (spec section 14).

Strategy start/stop are *cron* operations: "start" enables a strategy's schedules
(its crons are installed so the runner can trigger runs) and "stop" disables them.
The platform never launches a strategy process directly -- only the generic
runner, invoked by cron, does (spec 10, 11) -- and it never kills a run
*automatically* (spec 18/22). The one deliberate exception is **halt**: an
explicit, user-initiated kill of the current run (long-running strategies such
as live tick consumers otherwise have no way to stop from the dashboard). Halt
kills the process, never the schedules, and never cancels broker orders.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import (get_copy_service, get_deploy_service, get_process_service,
                          get_scheduler_service, get_strategy_service)
from app.core.config import Settings, get_settings
from app.core.security import Principal, get_current_user, require_auth
from app.models.system import RuntimeStatus
from app.services.copy_service import CopyService
from app.services.deploy_service import DeployService, DeployError
from app.services.process_service import ProcessService
from app.services.scheduler_service import SchedulerService
from app.services.strategy_service import StrategyService, StrategyServiceError

router = APIRouter(prefix="/strategies", tags=["deployment"], dependencies=[Depends(require_auth)])


def _require_owner(strategy_id: str, principal: Principal,
                   svc: StrategyService) -> None:
    """Ownership check — a user may only touch their own strategy."""
    try:
        svc.get_for_user(strategy_id, principal)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN if "access" in str(exc)
                            else status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/{strategy_id}/deploy")
def deploy(strategy_id: str, wait_if_running: bool = False,
           principal: Principal = Depends(get_current_user),
           svc: DeployService = Depends(get_deploy_service),
           strategies: StrategyService = Depends(get_strategy_service),
           copy: CopyService = Depends(get_copy_service)) -> dict:
    _require_owner(strategy_id, principal, strategies)
    try:
        result = svc.deploy(strategy_id, wait_if_running=wait_if_running)
    except DeployError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    # a deploy changes the marketplace disclosure (author_deployed = the
    # author has deployed at least once) — refresh the index entry inline
    # so followers see it immediately (the hourly rebuild is the backstop).
    try:
        copy.sync_entry(strategies.get(strategy_id))
    except Exception:  # noqa: BLE001 — cosmetic drift self-heals
        pass
    return result


@router.get("/{strategy_id}/status", response_model=RuntimeStatus)
def runtime_status(strategy_id: str, principal: Principal = Depends(get_current_user),
                   svc: ProcessService = Depends(get_process_service),
                   strategies: StrategyService = Depends(get_strategy_service)) -> RuntimeStatus:
    _require_owner(strategy_id, principal, strategies)
    return svc.runtime_status(strategy_id)


@router.post("/{strategy_id}/start")
def start(strategy_id: str, principal: Principal = Depends(get_current_user),
          svc: SchedulerService = Depends(get_scheduler_service),
          strategies: StrategyService = Depends(get_strategy_service)) -> dict:
    """Start a strategy = enable its cron schedules (crons are installed)."""
    _require_owner(strategy_id, principal, strategies)
    return svc.enable_strategy(strategy_id)


@router.post("/{strategy_id}/stop")
def stop(strategy_id: str, principal: Principal = Depends(get_current_user),
         svc: SchedulerService = Depends(get_scheduler_service),
         strategies: StrategyService = Depends(get_strategy_service)) -> dict:
    """Stop a strategy = disable its cron schedules so no further runs trigger."""
    _require_owner(strategy_id, principal, strategies)
    return svc.disable_strategy(strategy_id)


@router.post("/{strategy_id}/halt")
def halt(strategy_id: str, principal: Principal = Depends(get_current_user),
         proc: ProcessService = Depends(get_process_service),
         strategies: StrategyService = Depends(get_strategy_service)) -> dict:
    """Halt a strategy = kill the CURRENT run (SIGTERM → grace → SIGKILL on
    the process group).

    Deliberately distinct from ``stop`` (which only disables schedules — spec
    §14, "stop ≠ kill"): a live tick-consuming strategy runs through the whole
    session and needs an explicit way to die now. Halt is user-initiated (this
    endpoint is the only kill path; nothing in the platform halts on its own),
    it leaves the schedules untouched, and it never cancels broker orders —
    position management stays the strategy's own concern. The verdict is
    logged into the strategy's own log (the single sink, golden rule 4).
    """
    _require_owner(strategy_id, principal, strategies)
    result = proc.stop(strategy_id, graceful=True)
    if not result.get("stopped"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"strategy {strategy_id} is not running")
    # Halt means "stop this strategy NOW": its tick claims die with the
    # process (the user killed the run; there is never a live run during
    # the BTST overnight hold, so this releases nothing anyone needs).
    # Best-effort — the halt's own success is the primary verdict.
    tick_svc = getattr(proc, "tick_service", None)
    if tick_svc is not None:
        released = tick_svc.release_strategy_claims(strategy_id)
        if released:
            proc._log(strategy_id,
                      f"HALT: tick claims released: {released}", tag="PROCESS")
    sig = result.get("signal", "?")
    forced = " (forced SIGKILL after the grace period)" if result.get("forced") else ""
    proc._log(strategy_id, f"HALT: run terminated by the user ({sig}{forced}) "
                           f"— schedules were not changed", tag="PROCESS")
    return {"strategy_id": strategy_id, **result}


@router.post("/{strategy_id}/restart")
def restart(strategy_id: str, principal: Principal = Depends(get_current_user),
            svc: SchedulerService = Depends(get_scheduler_service),
            strategies: StrategyService = Depends(get_strategy_service)) -> dict:
    """Re-sync a strategy's crons (disable then enable)."""
    _require_owner(strategy_id, principal, strategies)
    svc.disable_strategy(strategy_id)
    return svc.enable_strategy(strategy_id)


@router.post("/{strategy_id}/run")
def run_now(strategy_id: str, body: dict | None = None,
            principal: Principal = Depends(get_current_user),
            svc: SchedulerService = Depends(get_scheduler_service),
            strategies: StrategyService = Depends(get_strategy_service),
            settings: Settings = Depends(get_settings)) -> dict:
    """Trigger a run NOW, through exactly the same path cron uses.

    The runner script is invoked as a subprocess with the platform's cron
    environment (so it sees the same bucket/paths), and its verdict is
    returned — ``STARTED pid=…``, ``SKIP: …`` or ``ERROR: …``. The strategy
    itself keeps running detached (the runner launches it with its own venv).

    This is a debugging trigger, not a second launcher: the runner remains the
    only thing that starts a strategy (spec §11).
    """
    import subprocess

    body = body or {}
    _require_owner(strategy_id, principal, strategies)
    try:
        doc = svc.strategy_service.get(strategy_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"strategy {strategy_id} not found") from exc
    schedule_id = body.get("schedule_id")
    args = (body.get("args") or "").strip()

    backend_dir = Path(settings.platform_root) / "platform" / "backend"
    runner = backend_dir / "scripts" / "runner.py"
    # The platform venv is `venv` in production (spec §4) and `.venv` in local
    # development — accept both.
    python = next((p for p in (backend_dir / "venv" / "bin" / "python",
                                backend_dir / ".venv" / "bin" / "python")
                   if p.exists()), None)
    if not runner.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, f"runner not found at {runner}")
    if python is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"platform venv not found in {backend_dir} (venv/bin/python)")

    runner_cmd = [str(python), str(runner), "--strategy-id", strategy_id]
    if schedule_id:
        # An explicit schedule: its args are used and its state is checked.
        runner_cmd += ["--schedule-id", schedule_id]
    else:
        enabled = [s for s in doc.schedules if s.enabled]
        if enabled:
            runner_cmd += ["--schedule-id", enabled[0].schedule_id]
        else:
            # No schedule at all → a manual run (the runner's --manual mode:
            # no schedule state check, no schedule args). A strategy must be
            # runnable on demand even before it is scheduled.
            runner_cmd += ["--manual"]
    if args:
        # Explicit args for this run override the schedule's command.
        runner_cmd += ["--command", args]

    # The same environment the scheduled jobs inherit from the backend process
    # (the in-process scheduler runs inside the service's own environment).
    env = dict(os.environ)
    env.update({
        "ENVIRONMENT": settings.environment,
        "AWS_REGION": settings.region,
        "S3_BUCKET": settings.s3_bucket,
        "PLATFORM_ROOT": str(settings.platform_root),
        "STRATEGIES_DIR": str(settings.strategies_dir_path()),
        "PYENV_ROOT": str(settings.pyenv_root),
        "PATH": f"{settings.pyenv_root}/shims:/usr/local/bin:/usr/bin:/bin",
    })
    try:
        proc = subprocess.run(
            runner_cmd,
            capture_output=True, text=True, timeout=120, env=env,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT,
                            "the runner did not respond within 120s")
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return {"strategy_id": strategy_id, "schedule_id": schedule_id,
            "manual": not schedule_id, "args": args,
            "exit_code": proc.returncode, "output": output[-4000:]}
