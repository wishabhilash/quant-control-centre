"""In-process scheduler for broker token refreshes (APScheduler + SQLite).

Dhan access tokens expire daily, so each connected user gets a job at
``BROKER_TOKEN_HOUR`` (default 08:00) in the platform timezone. The scheduler
runs inside the backend process; its job bookkeeping (job id + next run time
only — never credentials) lives in a small SQLite file under ``runtime/`` so
jobs survive a service restart.

Note on golden rule 1: this SQLite file is the scheduler's *own* bookkeeping,
not a platform data store — broker connection state stays in S3, credentials
in Parameter Store (SSM).
"""
from __future__ import annotations

import threading
from typing import Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger("services.broker_scheduler")

# Unique, stable job id per user *and* broker (APScheduler requires one).
JOB_ID_FMT = "{broker}-token-{user_id}"


def job_id(broker: str, user_id: str) -> str:
    return JOB_ID_FMT.format(broker=broker, user_id=user_id)


def run_token_refresh(user_id: str, broker: str = "dhan") -> None:
    """The scheduled job body.

    A module-level function so APScheduler can serialize the job by reference
    into the SQLite store and import it again on restart.
    """
    from app.api.deps import get_broker_service

    get_broker_service().refresh_token(user_id, broker)


class BrokerScheduler:
    """Lazily started BackgroundScheduler with a persistent SQLite job store."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._scheduler = None  # created by start(), not at construction
        self._start_lock = threading.Lock()

    # --- lifecycle ----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.settings.broker_scheduler_enabled and \
            self.settings.environment != "test"

    def start(self) -> None:
        """Start the scheduler (idempotent; a no-op in tests / when disabled)."""
        if not self.enabled or self._scheduler is not None:
            return
        with self._start_lock:
            if self._scheduler is not None:
                return
            from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
            from apscheduler.schedulers.background import BackgroundScheduler

            store_path = (self.settings.runtime_dir_path()
                          / "broker_scheduler.sqlite3").resolve()
            store_path.parent.mkdir(parents=True, exist_ok=True)
            self._scheduler = BackgroundScheduler(
                jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{store_path}")},
                timezone=self.settings.default_timezone,
            )
            self._scheduler.start()
            log.info("broker scheduler started (SQLite job store at %s, "
                     "timezone %s)", store_path, self.settings.default_timezone)

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    # --- jobs ---------------------------------------------------------------
    def schedule_daily(self, user_id: str, broker: str = "dhan",
                       hour: Optional[int] = None) -> None:
        """(Re-)register the daily token refresh for a user's broker.
        Idempotent."""
        if not self.enabled:
            log.info("broker scheduler disabled — token refresh for %s/%s not "
                     "scheduled (tokens must be regenerated manually)",
                     user_id, broker)
            return
        self.start()
        self._scheduler.add_job(
            run_token_refresh,
            trigger="cron",
            hour=self.settings.broker_token_hour if hour is None else hour,
            minute=0,
            id=job_id(broker, user_id),
            kwargs={"user_id": user_id, "broker": broker},
            replace_existing=True,
            # A backend restart around 08:00 must not silently skip the day.
            misfire_grace_time=3600,
        )
        log.info("daily %s token refresh scheduled for %s at %02d:00 %s",
                 broker, user_id,
                 self.settings.broker_token_hour if hour is None else hour,
                 self.settings.default_timezone)

    def unschedule(self, user_id: str, broker: str = "dhan") -> None:
        """Remove the user's job (no-op when the scheduler never ran)."""
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(job_id(broker, user_id))
        except Exception:  # noqa: BLE001 — job already gone
            pass

    def next_run(self, user_id: str, broker: str = "dhan") -> Optional[str]:
        """ISO time of the job's next scheduled refresh (None if not scheduled)."""
        if self._scheduler is None:
            return None
        try:
            job = self._scheduler.get_job(job_id(broker, user_id))
        except Exception:  # noqa: BLE001
            return None
        if job is None or job.next_run_time is None:
            return None
        return job.next_run_time.isoformat()
