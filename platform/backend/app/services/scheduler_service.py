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
from app.core.paths import UnownedDirectoryError, ensure_managed_dir
from app.models.strategy import Schedule, StrategyDocument
from app.services.strategy_service import StrategyService

log = get_logger("services.scheduler")

# A single cron line invokes the generic platform runner with the owning
# strategy id and the schedule id; the runner's output lands in the strategy's
# own directory so concurrent strategies never share a log sink.
RUNNER_TEMPLATE = (
    "{venv_python} {runner} --strategy-id {strategy_id} "
    "--schedule-id {schedule_id} >> {log_path} 2>&1"
)

# The platform's region inside a user's crontab: only lines between the markers
# are ever replaced; anything else in the crontab is left untouched.
CRON_BLOCK_BEGIN = "# BEGIN Quant Strategy Control Centre (managed — do not edit)"
CRON_BLOCK_END = "# END Quant Strategy Control Centre"


class SchedulerService:
    def __init__(self, settings: Settings | None = None, strategy_service=None):
        self.settings = settings or get_settings()
        self.strategy_service = strategy_service or StrategyService()

    @property
    def cron_file(self) -> Path:
        return self.settings.platform_cron_file_path()

    def runner_paths(self) -> tuple[str, str]:
        """(venv_python, runner script) for the cron line.

        Derived from ``PLATFORM_ROOT`` (spec §4 layout) instead of hard-coded
        paths, so a locally generated line is actually runnable: EC2 resolves to
        ``/opt/quant/platform/backend/venv/bin/python``; local dev (where the
        bootstrap uses ``.venv``) resolves to the ``.venv`` sibling.
        """
        backend = self.settings.resolved_platform_root() / "platform" / "backend"
        venv_python = backend / "venv" / "bin" / "python"
        if not venv_python.exists() and (backend / ".venv" / "bin" / "python").exists():
            venv_python = backend / ".venv" / "bin" / "python"
        return str(venv_python), str(backend / "scripts" / "runner.py")

    def _runner_command(self, strategy_id: str, schedule_id: str) -> str:
        venv_python, runner = self.runner_paths()
        # Per-strategy log (next to the strategy project, spec §4 layout) so the
        # cron output of different strategies never mixes.
        log_path = f"{self.settings.strategies_dir_path()}/{strategy_id}/runner.log"
        return RUNNER_TEMPLATE.format(venv_python=venv_python, runner=runner,
                                      strategy_id=strategy_id,
                                      schedule_id=schedule_id, log_path=log_path)

    def _time_prefix_for_tz(self, tz: str) -> str:
        """The per-schedule ``CRON_TZ`` line (spec §11).

        cronie (Amazon Linux, the production host) honours a ``CRON_TZ=<tz>``
        line on its own, applying to the crontab entries that follow it. We emit
        it before *every* entry (see ``build_cron_entry``) so each schedule's
        timezone is explicit and independent of the host's system timezone. On
        crons without ``CRON_TZ`` support (Debian, macOS) the line degrades to a
        harmless environment assignment and the schedule runs in host-local
        time — production therefore runs on cronie.
        """
        return f"CRON_TZ={tz}"

    def build_cron_entry(self, strategy_id: str, schedule: Schedule, doc: Optional[StrategyDocument] = None) -> str:
        """Build the crontab lines that invoke the generic runner for a schedule.

        Two lines: the schedule's ``CRON_TZ`` (own line — prefixing it onto the
        entry line would be executed as a command by the cron shell) followed by
        the entry itself. The entry only carries the strategy and schedule ids;
        the arguments the runner passes to the entry point come from
        ``schedule.command`` (read from S3 at fire time, the source of truth —
        golden rule 1), split into an argument array with no shell (golden rule 6).
        """
        cmd = self._runner_command(strategy_id, schedule.schedule_id)
        tz_line = self._time_prefix_for_tz(schedule.timezone)
        return f"{tz_line}\n{schedule.cron_expression} {cmd}"

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
            # The cron line redirects into {strategies}/{strategy_id}/runner.log;
            # create the directory up front so the shell redirect never fails on
            # a strategy that is scheduled but not yet deployed — created as
            # platform-owned (marked). An existing directory is left untouched.
            strategy_dir = Path(self.settings.strategies_dir_path()) / e["strategy_id"]
            if not strategy_dir.exists():
                try:
                    ensure_managed_dir(strategy_dir)
                except UnownedDirectoryError as exc:
                    log.error("cannot create strategy dir for %s: %s", e["strategy_id"], exc)
                    continue

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
        """Install the platform block into the system crontab.

        Merge semantics: only the lines between the managed-block markers are
        replaced; every unrelated entry in the user's crontab is preserved. The
        merged content is piped to ``crontab -`` on stdin (previously this
        passed no input, so the platform lines were never actually installed).
        """
        import shutil
        import subprocess
        crontab_bin = shutil.which("crontab")
        if crontab_bin is None:
            return {"installed": False, "reason": "crontab binary not found"}
        try:
            current = subprocess.run([crontab_bin, "-l"], capture_output=True,
                                     text=True, check=True).stdout
        except subprocess.CalledProcessError:
            current = ""  # empty crontab
        merged = self._merge_crontab(current, self.cron_file.read_text(encoding="utf-8"))
        cmd = [crontab_bin, "-"]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, input=merged)
            return {"installed": True}
        except subprocess.CalledProcessError as exc:
            log.error("failed to install crontab: %s", exc)
            return {"installed": False, "reason": str(exc)}

    @staticmethod
    def _merge_crontab(current: str, managed: str) -> str:
        """Replace only the managed block inside ``current``; keep the rest.

        When ``managed`` contains no cron entries, the whole block (markers
        included) is removed — an empty crontab contribution installs nothing.
        """
        import re
        pattern = re.compile(
            rf"^{re.escape(CRON_BLOCK_BEGIN)}\n.*?^{re.escape(CRON_BLOCK_END)}\n?",
            re.DOTALL | re.MULTILINE,
        )
        kept = pattern.sub("", current)
        if kept and not kept.endswith("\n"):
            kept += "\n"
        has_entries = any(
            line.strip() and not line.lstrip().startswith("#")
            for line in managed.splitlines()
        )
        if not has_entries:
            return kept
        return f"{kept}{CRON_BLOCK_BEGIN}\n{managed}{CRON_BLOCK_END}\n"

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
