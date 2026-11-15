"""In-process scheduler for strategy schedules (APScheduler + SQLite).

Strategy schedules are no longer system crontab entries: the backend owns a
``BackgroundScheduler`` whose jobs invoke the **same generic runner**
(``scripts/runner.py``) cron used to — the two-stage chain is unchanged, only
the trigger moved in-process (spec §10, §11). Cron expressions, timezones and
enable flags come from the schedule documents in S3 (golden rule 1);
``SchedulerService.reconcile`` diffs the desired job set against the store on
every schedule change, and the startup reconcile rebuilds everything from S3.

The job bookkeeping (job id + next run time only — never strategy data) lives
in a small SQLite file under ``runtime/`` so jobs survive a restart; like the
broker scheduler's file, it is the scheduler's *own* bookkeeping, not a
platform data store. In dry-run mode (``SCHEDULER_DRY_RUN``, the local default)
the scheduler never starts, so local development and tests fire nothing — the
same guarantee the crontab dry-run used to give.
"""
from __future__ import annotations

import re
import subprocess
import threading
import zoneinfo
from pathlib import Path
from typing import Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.paths import ensure_managed_dir

log = get_logger("services.strategy_scheduler")

# Unique, stable job id per strategy *and* schedule (schedule ids are only
# unique per strategy). Both parts match the STR-/SCH- id patterns, so the id
# is parseable back into its parts for the status endpoint.
JOB_ID_FMT = "strategy-{strategy_id}-{schedule_id}"
_JOB_ID_RE = re.compile(r"^strategy-(STR-[A-Za-z0-9]+)-(SCH-[A-Za-z0-9]+)$")


def job_id(strategy_id: str, schedule_id: str) -> str:
    return JOB_ID_FMT.format(strategy_id=strategy_id, schedule_id=schedule_id)


def parse_job_id(job_id_str: str) -> Optional[tuple[str, str]]:
    """(strategy_id, schedule_id) for a managed job id, else None."""
    m = _JOB_ID_RE.match(job_id_str)
    return (m.group(1), m.group(2)) if m else None


def runner_paths(settings: Settings) -> tuple[str, str]:
    """(venv_python, runner script) the scheduled job invokes.

    Derived from ``PLATFORM_ROOT`` (spec §4 layout) instead of hard-coded
    paths: EC2 resolves to ``/opt/quant-control-centre/platform/backend/
    venv/bin/python``; local dev (whose bootstrap uses ``.venv``) resolves to
    the ``.venv`` sibling.
    """
    backend = settings.resolved_platform_root() / "platform" / "backend"
    venv_python = backend / "venv" / "bin" / "python"
    if not venv_python.exists() and (backend / ".venv" / "bin" / "python").exists():
        venv_python = backend / ".venv" / "bin" / "python"
    return str(venv_python), str(backend / "scripts" / "runner.py")


def _cron_dow_to_apscheduler(field: str) -> str:
    """Translate a cron day-of-week field into APScheduler's numbering.

    cron (and croniter) number the days **0/7=Sunday … 6=Saturday**, while
    APScheduler uses **0=Monday … 6=Sunday** — passing a cron field straight
    through would shift every weekday by one (a "Mon–Fri" schedule firing
    Tue–Sat). Named values (``mon-fri``) are valid in both and pass unchanged;
    numeric tokens (including ranges, steps and the ``7``=Sunday alias) are
    expanded and re-emitted in APScheduler numbering.
    """
    def expand(tok: str) -> set[int]:
        if tok in ("*", "?"):
            return set(range(7))
        if tok.startswith("*/"):  # */n over the whole week
            return {v for v in range(7) if v % int(tok[2:]) == 0}
        if "/" in tok:
            base, step = tok.split("/", 1)
            start = int(base.split("-")[0]) if base != "*" else 0
            return {v for v in range(start, 7) if (v - start) % int(step) == 0}
        if "-" in tok:
            a, b = (int(x) for x in tok.split("-", 1))
            if a <= b:
                return set(range(a, b + 1))
            return set(range(a, 7)) | set(range(0, b + 1))  # cron wraps (5-1)
        return {int(tok)}

    values: set[int] = set()
    for tok in (t.strip() for t in field.split(",")):
        try:
            values |= expand(tok)
        except ValueError:
            return field  # names (mon, fri…) — valid in both numberings
    mapped = {(v - 1) % 7 for v in values}
    if mapped == set(range(7)):
        return "*"
    return ",".join(str(v) for v in sorted(mapped))


def cron_trigger(expression: str, timezone: str):
    """Build the APScheduler cron trigger for a schedule.

    Accepts the same expressions the platform always has (5 fields, or 6 when
    the first field is seconds — see the Schedule model). Raises ``ValueError``
    for an invalid expression or timezone, so ``reconcile`` can skip a broken
    schedule without refusing to schedule the others.
    """
    from apscheduler.triggers.cron import CronTrigger

    fields = expression.split()
    try:
        tz = zoneinfo.ZoneInfo(timezone)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"unknown timezone: {timezone}") from exc
    if len(fields) == 5:
        return CronTrigger(minute=fields[0], hour=fields[1], day=fields[2],
                           month=fields[3],
                           day_of_week=_cron_dow_to_apscheduler(fields[4]),
                           timezone=tz)
    if len(fields) == 6:  # 6-field cron: the first field is seconds
        return CronTrigger(second=fields[0], minute=fields[1], hour=fields[2],
                           day=fields[3], month=fields[4],
                           day_of_week=_cron_dow_to_apscheduler(fields[5]),
                           timezone=tz)
    raise ValueError(f"invalid cron expression (expected 5 or 6 fields): {expression!r}")


def run_schedule(strategy_id: str, schedule_id: str) -> None:
    """The scheduled job body: invoke the generic runner, as cron did.

    A module-level function so APScheduler can serialize the job by reference
    into the SQLite store and import it again on restart. The subprocess
    inherits the backend process's environment (the whole crontab SHELL/PATH/
    ENVIRONMENT preamble existed only because cron runs bare); the runner
    loads ``.env`` itself anyway. Its stdout/stderr lands in the strategy's
    own ``runner.log`` — never a shared sink (golden rule 4).

    Never raises: a failed tick is logged and the next fire proceeds.
    """
    settings = get_settings()
    venv_python, runner = runner_paths(settings)
    log_path = Path(settings.strategies_dir_path()) / strategy_id / "runner.log"
    try:
        ensure_managed_dir(log_path.parent)
    except Exception as exc:  # noqa: BLE001 — a foreign dir must not crash the job
        log.error("cannot create strategy dir for %s: %s", strategy_id, exc)
        return
    cmd = [venv_python, runner, "--strategy-id", strategy_id,
           "--schedule-id", schedule_id]
    log.info("schedule tick: %s", " ".join(cmd))
    try:
        with open(log_path, "ab") as fh:  # same redirect the cron line carried
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT)
    except Exception as exc:  # noqa: BLE001
        log.error("runner invocation failed for %s/%s: %s", strategy_id,
                  schedule_id, exc)
        return
    if proc.returncode != 0:
        log.error("runner exited %s for %s/%s (see %s)", proc.returncode,
                  strategy_id, schedule_id, log_path)


class StrategyScheduler:
    """Lazily started BackgroundScheduler with a persistent SQLite job store.

    One instance per process (``shared_scheduler``); the strategy CRUD,
    deploy and startup reconcile paths all funnel through it.
    """

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._scheduler = None  # created by start(), not at construction
        self._start_lock = threading.Lock()

    # --- lifecycle ----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return not self.settings.scheduler_dry_run and \
            self.settings.environment != "test"

    @property
    def job_store_path(self) -> Path:
        return (self.settings.runtime_dir_path()
                / "strategy_scheduler.sqlite3").resolve()

    def start(self) -> None:
        """Start the scheduler (idempotent; a no-op in tests / when disabled)."""
        if not self.enabled or self._scheduler is not None:
            return
        with self._start_lock:
            if self._scheduler is not None:
                return
            from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
            from apscheduler.schedulers.background import BackgroundScheduler

            store_path = self.job_store_path
            store_path.parent.mkdir(parents=True, exist_ok=True)
            self._scheduler = BackgroundScheduler(
                jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{store_path}")},
                timezone=self.settings.default_timezone,
            )
            self._scheduler.start()
            log.info("strategy scheduler started (SQLite job store at %s, "
                     "timezone %s)", store_path, self.settings.default_timezone)

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    # --- jobs ---------------------------------------------------------------
    def upsert(self, strategy_id: str, schedule) -> None:
        """(Re-)register the schedule's job. Idempotent (replace_existing)."""
        self.start()
        self._scheduler.add_job(
            run_schedule,
            trigger=cron_trigger(schedule.cron_expression, schedule.timezone),
            id=job_id(strategy_id, schedule.schedule_id),
            args=(strategy_id, schedule.schedule_id),
            replace_existing=True,
            # A backend restart must not silently skip a due fire (same rule
            # as the broker scheduler's 08:00 refresh).
            misfire_grace_time=3600,
        )
        log.info("scheduled %s/%s (%s, %s)", strategy_id, schedule.schedule_id,
                 schedule.cron_expression, schedule.timezone)

    def remove_job(self, job_id_str: str) -> None:
        """Remove a job (no-op when the scheduler never ran / job is gone)."""
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(job_id_str)
        except Exception:  # noqa: BLE001 — job already gone
            pass

    def job_ids(self) -> set[str]:
        if self._scheduler is None:
            return set()
        try:
            return {j.id for j in self._scheduler.get_jobs()}
        except Exception:  # noqa: BLE001
            return set()

    def next_run(self, job_id_str: str) -> Optional[str]:
        """ISO time of the job's next fire (None if not scheduled)."""
        if self._scheduler is None:
            return None
        try:
            job = self._scheduler.get_job(job_id_str)
        except Exception:  # noqa: BLE001
            return None
        if job is None or job.next_run_time is None:
            return None
        return job.next_run_time.isoformat()


# One scheduler per process: SchedulerService is constructed in several places
# (deps, deploy service, strategy service) but they must all drive the same
# BackgroundScheduler — two instances would fight over the SQLite job store.
_shared: Optional[StrategyScheduler] = None


def shared_scheduler() -> StrategyScheduler:
    global _shared
    if _shared is None:
        _shared = StrategyScheduler()
    return _shared
