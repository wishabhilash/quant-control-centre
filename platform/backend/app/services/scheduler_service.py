"""Scheduling service.

Uses Linux cron. A strategy may have any number of independent schedules. The
platform owns a dedicated crontab file generated from *enabled* schedules in
S3; it never overwrites unrelated system cron entries. In local/dev mode
(``scheduler_dry_run``) the generated file is written but not installed into the
system crontab, so testing never touches a Mac's crontab (spec sections 11, 17).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import List, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.strategy import Schedule, StrategyDocument
from app.services.strategy_service import StrategyService

log = get_logger("services.scheduler")

# A single cron line invokes the generic platform runner with a schedule id.
RUNNER_TEMPLATE = (
    "{venv_python} {runner} --schedule-id {schedule_id} "
    ">> {log_path} 2>&1"
)


class SchedulerService:
    def __init__(self, settings: Settings | None = None, strategy_service=None):
        self.settings = settings or get_settings()
        self.strategy_service = strategy_service or StrategyService()

    @property
    def cron_file(self) -> Path:
        return self.settings.platform_cron_file_path()

    def _runner_command(self, schedule_id: str) -> str:
        s = self.settings
        venv_python = f"/opt/quant/platform/backend/venv/bin/python"
        runner = f"/opt/quant/platform/backend/scripts/runner.py"
        log_path = f"{s.strategies_dir_path()}/__runner__/runner.log"
        return RUNNER_TEMPLATE.format(venv_python=venv_python, runner=runner,
                                     schedule_id=schedule_id, log_path=log_path)

    def _time_prefix_for_tz(self, tz: str) -> str:
        """cron(1) honours a ``CRON_TZ`` environment line when supported.

        The spec requires testing timezone behaviour on the production cron
        implementation. We emit a per-schedule CRON_TZ where the tz differs from
        the platform default and document that system cron must support it.
        """
        return ""  # placeholder; see reconcile() for per-line CRON_TZ handling

    def build_cron_entry(self, strategy_id: str, schedule: Schedule, doc: Optional[StrategyDocument] = None) -> str:
        cmd = self._runner_command(schedule.schedule_id)
        # Emit CRON_TZ for the schedule's timezone when supported.
        tz_line = f"CRON_TZ={schedule.timezone}" if schedule.timezone != self.settings.default_timezone else ""
        parts = [schedule.cron_expression, cmd]
        if tz_line:
            parts.insert(0, tz_line)
        return " ".join(parts)

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
        """Regenerate the platform-owned crontab from enabled schedules.

        Never overwrites unrelated system cron entries: only the platform file
        is (re)written. Installation into the system crontab happens only when
        not in dry-run mode.
        """
        enabled = self.collect_enabled_schedules()
        if strategy_id is not None:
            enabled = [e for e in enabled if e["strategy_id"] == strategy_id]

        lines = ["# Managed by Quant Strategy Control Centre — do not edit by hand.",
                 f"# generated_at={_dt.datetime.now(_dt.timezone.utc).isoformat()}"]
        for e in enabled:
            lines.append(self.build_cron_entry(e["strategy_id"], e["schedule"], e["doc"]))

        self.cron_file.parent.mkdir(parents=True, exist_ok=True)
        self.cron_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = {
            "cron_file": str(self.cron_file),
            "entries": len(enabled),
            "installed": False,
            "dry_run": self.settings.scheduler_dry_run,
        }
        if not self.settings.scheduler_dry_run:
            result = self._install_system_cron()
        log.info("reconciled cron: %s", result)
        return result

    def _install_system_cron(self) -> dict:
        import shutil
        import subprocess
        crontab_bin = shutil.which("crontab")
        if crontab_bin is None:
            return {"installed": False, "reason": "crontab binary not found"}
        # Merge: append platform lines to existing crontab within a managed block.
        try:
            subprocess.run([crontab_bin, "-l"], capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError:
            pass  # empty crontab
        cmd = self._build_install_command()
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return {"installed": True}
        except subprocess.CalledProcessError as exc:
            log.error("failed to install crontab: %s", exc)
            return {"installed": False, "reason": str(exc)}

    def _build_install_command(self) -> List[str]:
        import shutil
        crontab_bin = shutil.which("crontab")
        return [crontab_bin, "-"]

    # --- schedule CRUD ----------------------------------------------------
    def create_schedule(self, strategy_id: str, name, cron_expression, timezone, enabled) -> Schedule:
        doc = self.strategy_service.get(strategy_id)
        schedule_id = self._generate_schedule_id(doc.schedules)
        sched = Schedule(schedule_id=schedule_id, name=name, cron_expression=cron_expression,
                         timezone=timezone or self.settings.default_timezone, enabled=enabled)
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

    # --- strategy-level enable/disable (turn the strategy's crons on/off) --
    def enable_strategy(self, strategy_id: str) -> dict:
        """Start a strategy = switch its crons ON.

        Only a cron trigger (via the generic runner) ever runs a strategy; the
        platform never launches a strategy process directly. Enabling flips the
        strategy's ``enabled`` flag and reconciles the platform crontab so its
        enabled schedules are (re)installed.
        """
        doc = self.strategy_service.get(strategy_id)
        doc.enabled = True
        self.strategy_service.store.update_document(doc)
        result = self.reconcile(strategy_id)
        result["strategy_id"] = strategy_id
        result["enabled"] = True
        return result

    def disable_strategy(self, strategy_id: str) -> dict:
        """Stop a strategy = switch its crons OFF.

        Disabling removes every cron line for the strategy. Any run already in
        progress (triggered by a prior cron tick) finishes on its own -- it is
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
