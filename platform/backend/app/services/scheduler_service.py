"""Scheduling service.

Strategy schedules fire from an in-process APScheduler (see
``strategy_scheduler``) instead of the system crontab: the trigger moved, the
two-stage chain did not — each job invokes the generic runner
(``scripts/runner.py``) exactly as the cron line used to. A strategy may have
any number of independent schedules; ``reconcile`` diffs the desired job set
(enabled strategy × enabled schedule, from S3 — golden rule 1) against the
scheduler's job store. In dry-run mode (``SCHEDULER_DRY_RUN``, the local
default) nothing is scheduled, so testing never fires a run (spec §11, §17).
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import List, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.paths import UnownedDirectoryError, ensure_managed_dir
from app.models.strategy import Schedule, StrategyDocument
from app.services import strategy_scheduler as _ss
from app.services.strategy_scheduler import parse_job_id
from app.services.strategy_service import StrategyService

log = get_logger("services.scheduler")


class SchedulerService:
    def __init__(self, settings: Settings | None = None, strategy_service=None,
                 strategy_scheduler=None):
        self.settings = settings or get_settings()
        self.strategy_service = strategy_service or StrategyService()
        # The process-wide scheduler (shared, never per-instance — two
        # BackgroundSchedulers would fight over the SQLite job store).
        self.strategy_scheduler = strategy_scheduler or _ss.shared_scheduler()
        # The outcome of the last reconcile (registered / dry-run / skipped) so
        # the dashboard can explain why a schedule does or does not fire.
        self.last_reconcile: dict = {}

    def runner_paths(self) -> tuple[str, str]:
        """(venv_python, runner script) the scheduled jobs invoke."""
        return _ss.runner_paths(self.settings)

    def collect_enabled_schedules(self) -> List[dict]:
        """Return [{strategy_id, schedule, doc}] for all enabled, enabled-strategy schedules."""
        out = []
        for doc in self.strategy_service.store.list_documents():
            if not doc.enabled:
                continue
            for s in doc.schedules:
                if s.enabled:
                    out.append({"strategy_id": doc.strategy_id, "schedule": s, "doc": doc})
        return out

    def reconcile(self, strategy_id: Optional[str] = None) -> dict:
        """Sync the scheduler's jobs with the enabled schedules in S3.

        Desired set = enabled schedules of enabled strategies (optionally
        scoped to one strategy). Jobs for schedules that were disabled,
        deleted or given an invalid expression are removed / never added; a
        schedule with an unparseable cron expression is *skipped* (reported in
        the result), never allowed to break the rest. In dry-run mode nothing
        is registered — the result only reports what *would* be scheduled.
        """
        enabled = self.collect_enabled_schedules()
        if strategy_id is not None:
            enabled = [e for e in enabled if e["strategy_id"] == strategy_id]

        desired = {}
        skipped = []
        for e in enabled:
            schedule = e["schedule"]
            try:
                _ss.cron_trigger(schedule.cron_expression, schedule.timezone)
            except Exception as exc:  # noqa: BLE001 — one bad cron must not sink the rest
                skipped.append({"strategy_id": e["strategy_id"],
                                "schedule_id": schedule.schedule_id,
                                "reason": str(exc)})
                log.warning("schedule %s/%s not scheduled: %s",
                            e["strategy_id"], schedule.schedule_id, exc)
                continue
            desired[_ss.job_id(e["strategy_id"], schedule.schedule_id)] = e

        result = {"mode": "apscheduler", "jobs": len(desired), "added": 0,
                  "removed": 0, "skipped": skipped,
                  "dry_run": self.settings.scheduler_dry_run}
        scheduler = self.strategy_scheduler
        if self.settings.scheduler_dry_run or not scheduler.enabled:
            result["mode"] = "dry_run"
            result["note"] = ("dry-run: schedules stay in S3, the in-process "
                              "scheduler is not running")
        else:
            scheduler.start()
            current = {jid for jid in scheduler.job_ids() if parse_job_id(jid)}
            if strategy_id is not None:
                # Scoped reconcile: only ever touch this strategy's own jobs.
                prefix = f"strategy-{strategy_id}-"
                current = {jid for jid in current if jid.startswith(prefix)}
            for jid in sorted(current - set(desired)):
                scheduler.remove_job(jid)
                result["removed"] += 1
            for jid, e in desired.items():
                scheduler.upsert(e["strategy_id"], e["schedule"])
                result["added"] += 1
            # Pre-create each scheduled strategy's directory (platform-owned)
            # so the runner's log redirect in the job body cannot fail on a
            # scheduled-but-not-yet-deployed strategy. Existing dirs untouched.
            for e in desired.values():
                strategy_dir = Path(self.settings.strategies_dir_path()) / e["strategy_id"]
                if not strategy_dir.exists():
                    try:
                        ensure_managed_dir(strategy_dir)
                    except UnownedDirectoryError as exc:
                        log.error("cannot create strategy dir for %s: %s",
                                  e["strategy_id"], exc)
        # Remember the outcome so the dashboard can explain the scheduler state.
        self.last_reconcile = dict(result)
        log.info("reconciled schedules: %s", result)
        return result

    def scheduled_overview(self) -> List[dict]:
        """The currently registered schedule jobs (+ next fire time) for the status endpoint."""
        out = []
        for jid in sorted(self.strategy_scheduler.job_ids()):
            ids = parse_job_id(jid)
            if ids is None:
                continue
            out.append({"job_id": jid, "strategy_id": ids[0],
                        "schedule_id": ids[1],
                        "next_run": self.strategy_scheduler.next_run(jid)})
        return out

    # --- schedule CRUD ----------------------------------------------------
    def create_schedule(self, strategy_id: str, name, cron_expression, timezone, enabled,
                       command: str = "") -> Schedule:
        doc = self.strategy_service.get(strategy_id)
        schedule_id = self._generate_schedule_id(doc.schedules)
        sched = Schedule(schedule_id=schedule_id, name=name, cron_expression=cron_expression,
                         timezone=timezone or self.settings.default_timezone, enabled=enabled,
                         command=command or "")
        doc.schedules.append(sched)
        self.strategy_service.store.update_document(doc)
        return sched

    def update_schedule(self, strategy_id, schedule_id, patch) -> Schedule:
        doc = self.strategy_service.get(strategy_id)
        sched = self._find_schedule(doc, schedule_id)
        if patch.name is not None:
            sched.name = patch.name
        if patch.cron_expression is not None:
            sched.cron_expression = patch.cron_expression
        if patch.timezone is not None:
            sched.timezone = patch.timezone
        if patch.enabled is not None:
            sched.enabled = patch.enabled
        if patch.command is not None:
            sched.command = patch.command
        self.strategy_service.store.update_document(doc)
        return sched

    def delete_schedule(self, strategy_id, schedule_id) -> None:
        doc = self.strategy_service.get(strategy_id)
        doc.schedules = [s for s in doc.schedules if s.schedule_id != schedule_id]
        self.strategy_service.store.update_document(doc)

    def enable_schedule(self, strategy_id, schedule_id) -> Schedule:
        doc = self.strategy_service.get(strategy_id)
        self._find_schedule(doc, schedule_id).enabled = True
        self.strategy_service.store.update_document(doc)
        return doc

    def disable_schedule(self, strategy_id, schedule_id) -> None:
        doc = self.strategy_service.get(strategy_id)
        sched = self._find_schedule(doc, schedule_id)
        if sched is not None:
            sched.enabled = False
            self.strategy_service.store.update_document(doc)

    # --- strategy-level enable/disable (turn the strategy's schedules on/off)
    def enable_strategy(self, strategy_id: str) -> dict:
        """Start a strategy = switch its schedules ON.

        Only the scheduled runner ever runs a strategy; the platform never
        launches a strategy process directly. Enabling flips the strategy's
        ``enabled`` flag and reconciles the scheduler so its enabled schedules
        are (re)registered.
        """
        doc = self.strategy_service.get(strategy_id)
        doc.enabled = True
        self.strategy_service.store.update_document(doc)
        result = self.reconcile(strategy_id)
        result["strategy_id"] = strategy_id
        result["enabled"] = True
        return result

    def disable_strategy(self, strategy_id: str) -> dict:
        """Stop a strategy = switch its schedules OFF.

        Disabling removes the strategy's scheduled jobs. Any run already in
        progress (triggered by a prior tick) finishes on its own -- it is
        never auto-killed and never auto-closes broker positions (spec 18/22).
        """
        doc = self.strategy_service.get(strategy_id)
        doc.enabled = False
        self.strategy_service.store.update_document(doc)
        result = self.reconcile(strategy_id)
        result["strategy_id"] = strategy_id
        result["enabled"] = False
        return result

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _generate_schedule_id(existing: List[Schedule]) -> str:
        used = {s.schedule_id for s in existing}
        n = 1
        while True:
            candidate = f"SCH-{n:03d}"
            if candidate not in used:
                return candidate
            n += 1

    @staticmethod
    def _find_schedule(doc, schedule_id) -> Optional[Schedule]:
        for s in doc.schedules:
            if s.schedule_id == schedule_id:
                return s
        raise ValueError(f"schedule not found: {schedule_id}")

    def next_execution(self, schedule: Schedule) -> Optional[str]:
        """Best-effort next fire time using croniter when available."""
        try:
            from croniter import croniter

            import zoneinfo
            tz = zoneinfo.ZoneInfo(schedule.timezone)
            base = _dt.datetime.now(tz)
            nxt = croniter(schedule.cron_expression, base).get_next(_dt.datetime)
            return nxt.isoformat()
        except Exception:  # noqa: BLE001
            return None

    def upcoming(self, cron_expression: str, timezone: Optional[str] = None,
                 count: int = 5) -> List[str]:
        """The next ``count`` fire times (ISO) of a cron expression.

        Powers the schedule editor's "upcoming runs" preview (like AWS
        EventBridge's next-occurrences list) so an operator can see what a
        cron expression really means before saving it.

        Raises ``ValueError`` for an invalid expression or timezone.
        """
        from croniter import croniter

        import zoneinfo
        try:
            tz = zoneinfo.ZoneInfo(timezone or self.settings.default_timezone)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"unknown timezone: {timezone}") from exc
        count = max(1, min(int(count or 5), 20))
        base = _dt.datetime.now(tz)
        try:
            it = croniter(cron_expression, base)
            return [it.get_next(_dt.datetime).isoformat() for _ in range(count)]
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid cron expression: {cron_expression}") from exc
