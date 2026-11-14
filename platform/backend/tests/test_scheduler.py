"""Scheduler enable/disable (cron-only trigger model, spec 11 / 19).

A strategy is only ever run by its cron via the generic runner; the platform's
start/stop operations enable/disable those crons rather than launching a process.
"""
from app.models.strategy import StrategyCreate, StrategyUpdate, ScheduleUpdate
from app.services.scheduler_service import (
    CRON_BLOCK_BEGIN,
    CRON_BLOCK_END,
    SchedulerService,
)

def _create(strategy_service):
    return strategy_service.create(
        StrategyCreate(name="Gamma", files={"main.py": "print(1)"})
    )


def _add_schedule(scheduler_service: SchedulerService, strategy_id: str) -> str:
    sched = scheduler_service.create_schedule(strategy_id, "hourly", "0 * * * *", "Asia/Kolkata", True)
    return sched.schedule_id


def test_enable_strategy_installs_cron(scheduler_service):
    doc = _create(scheduler_service.strategy_service)
    sid = _add_schedule(scheduler_service, doc.strategy_id)

    # Start a strategy = enable its crons.
    res = scheduler_service.enable_strategy(doc.strategy_id)
    assert res["enabled"] is True
    assert sid in scheduler_service.cron_file.read_text()
    # The stored document is now enabled.
    assert scheduler_service.strategy_service.get(doc.strategy_id).enabled is True


def test_disable_strategy_removes_cron(scheduler_service):
    doc = _create(scheduler_service.strategy_service)
    sid = _add_schedule(scheduler_service, doc.strategy_id)
    scheduler_service.enable_strategy(doc.strategy_id)
    assert sid in scheduler_service.cron_file.read_text()

    # Stop a strategy = disable its crons; the runner will no longer trigger it.
    res = scheduler_service.disable_strategy(doc.strategy_id)
    assert res["enabled"] is False
    assert sid not in scheduler_service.cron_file.read_text()
    assert scheduler_service.strategy_service.get(doc.strategy_id).enabled is False


def test_restart_resyncs_cron(scheduler_service):
    doc = _create(scheduler_service.strategy_service)
    sid = _add_schedule(scheduler_service, doc.strategy_id)
    scheduler_service.disable_strategy(doc.strategy_id)
    assert sid not in scheduler_service.cron_file.read_text()
    scheduler_service.enable_strategy(doc.strategy_id)
    assert sid in scheduler_service.cron_file.read_text()


def test_update_enabled_reconciles_cron(scheduler_service):
    doc = _create(scheduler_service.strategy_service)
    sid = _add_schedule(scheduler_service, doc.strategy_id)
    scheduler_service.enable_strategy(doc.strategy_id)
    assert sid in scheduler_service.cron_file.read_text()

    # Toggling enabled via update() must reconcile the crontab too.
    scheduler_service.strategy_service.update(doc.strategy_id, StrategyUpdate(enabled=False))
    assert sid not in scheduler_service.cron_file.read_text()
    scheduler_service.strategy_service.update(doc.strategy_id, StrategyUpdate(enabled=True))
    assert sid in scheduler_service.cron_file.read_text()


def test_create_schedule_stores_command(scheduler_service):
    """A per-schedule ``command`` is persisted so the runner can pass it as args."""
    doc = _create(scheduler_service.strategy_service)
    sched = scheduler_service.create_schedule(
        doc.strategy_id, "backtest", "0 * * * *", "Asia/Kolkata", True,
        command="--mode backtest --date 2024-01-01",
    )
    assert sched.command == "--mode backtest --date 2024-01-01"
    # Round-trips through the store (the source of truth the runner reads).
    reloaded = scheduler_service.strategy_service.get(doc.strategy_id)
    assert reloaded.schedules[0].command == "--mode backtest --date 2024-01-01"


def test_schedule_command_defaults_empty(scheduler_service):
    doc = _create(scheduler_service.strategy_service)
    sched = scheduler_service.create_schedule(doc.strategy_id, "x", "0 * * * *", "Asia/Kolkata", True)
    assert sched.command == ""


def test_update_schedule_command(scheduler_service):
    doc = _create(scheduler_service.strategy_service)
    sched = scheduler_service.create_schedule(doc.strategy_id, "x", "0 * * * *", "Asia/Kolkata", True)
    updated = scheduler_service.update_schedule(
        doc.strategy_id, sched.schedule_id, ScheduleUpdate(command="--live")
    )
    assert updated.command == "--live"
    # Updating one field must not wipe the others.
    assert updated.name == "x"
    assert updated.enabled is True


def test_cron_entry_uses_per_strategy_log(scheduler_service):
    """Runner output lands in the strategy's own dir, never a shared sink."""
    doc = _create(scheduler_service.strategy_service)
    sched = scheduler_service.create_schedule(doc.strategy_id, "x", "0 * * * *", "Asia/Kolkata", True)
    entry = scheduler_service.build_cron_entry(doc.strategy_id, sched)
    # The line carries both ids so the runner resolves the schedule within the
    # strategy (SCH ids are only unique per strategy).
    assert f"--strategy-id {doc.strategy_id}" in entry
    assert f"--schedule-id {sched.schedule_id}" in entry
    assert f"/{doc.strategy_id}/runner.log" in entry
    assert "__runner__" not in entry
    # The timezone is explicit: CRON_TZ on its OWN line before the entry (the
    # same-line prefix would be executed as a command by the cron shell).
    first, second = entry.splitlines()
    assert first == "CRON_TZ=Asia/Kolkata"
    assert second.startswith("0 * * * * ")
    # Reconcile pre-creates the log directory for the shell redirect.
    scheduler_service.reconcile(doc.strategy_id)
    assert (scheduler_service.settings.strategies_dir_path() / doc.strategy_id).is_dir()


def test_runner_paths_are_settings_derived(scheduler_service):
    """The cron line's venv/runner paths follow PLATFORM_ROOT, not /opt/quant."""
    doc = _create(scheduler_service.strategy_service)
    sched = scheduler_service.create_schedule(doc.strategy_id, "x", "0 * * * *", "Asia/Kolkata", True)
    entry = scheduler_service.build_cron_entry(doc.strategy_id, sched)
    root = str(scheduler_service.settings.resolved_platform_root())
    assert root in entry
    assert "/opt/quant" not in entry
    python_path, runner_path = scheduler_service.runner_paths()
    assert f"{python_path} {runner_path} " in entry


def test_cron_entry_uses_nondefault_timezone_line(scheduler_service):
    """A non-default timezone gets its own CRON_TZ line too (never inline)."""
    doc = _create(scheduler_service.strategy_service)
    sched = scheduler_service.create_schedule(doc.strategy_id, "ny", "0 9 * * *", "America/New_York", True)
    entry = scheduler_service.build_cron_entry(doc.strategy_id, sched)
    first, second = entry.splitlines()
    assert first == "CRON_TZ=America/New_York"
    assert second.startswith("0 9 * * * ")


def test_cron_file_declares_shell_and_path(scheduler_service, settings):
    """The service account's shell is nologin — cron must be told which shell to
    use, and the PATH must carry the pyenv shims."""
    doc = _create(scheduler_service.strategy_service)
    _add_schedule(scheduler_service, doc.strategy_id)
    scheduler_service.reconcile(doc.strategy_id)
    content = scheduler_service.cron_file.read_text()
    assert "SHELL=/bin/bash" in content
    assert f"PATH={settings.pyenv_root}/shims" in content


def test_cron_file_carries_the_platform_environment(scheduler_service, settings):
    """cron does not inherit the service environment — the runner must still see
    the right bucket/region/paths, so they are written into the crontab."""
    doc = _create(scheduler_service.strategy_service)
    _add_schedule(scheduler_service, doc.strategy_id)
    scheduler_service.reconcile(doc.strategy_id)
    content = scheduler_service.cron_file.read_text()
    assert f"ENVIRONMENT={settings.environment}" in content
    assert f"AWS_REGION={settings.region}" in content
    assert f"S3_BUCKET={settings.s3_bucket}" in content
    assert f"PLATFORM_ROOT={settings.platform_root}" in content
    assert f"STRATEGIES_DIR={settings.strategies_dir_path()}" in content
    assert f"PYENV_ROOT={settings.pyenv_root}" in content


def test_upcoming_runs_preview(scheduler_service):
    """The editor previews the upcoming runs (like EventBridge's next occurrences)."""
    runs = scheduler_service.upcoming("*/5 * * * *", "Asia/Kolkata", 5)
    assert len(runs) == 5
    # ISO timestamps, in the requested timezone.
    assert runs[0].startswith("20")
    assert "+05:30" in runs[0]


def test_upcoming_runs_rejects_invalid_cron(scheduler_service):
    import pytest

    with pytest.raises(ValueError):
        scheduler_service.upcoming("not a cron", "Asia/Kolkata", 5)


def test_upcoming_runs_shows_the_day_of_week_surprise(scheduler_service):
    """`1-5` is Monday–Friday: the preview must make the weekend gap visible."""
    import datetime as _dt
    import zoneinfo

    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    runs = scheduler_service.upcoming("0 9 * * 1-5", "Asia/Kolkata", 10)
    weekdays = {_dt.datetime.fromisoformat(r).astimezone(tz).weekday() for r in runs}
    # Monday(0) .. Friday(4) only — never Saturday/Sunday.
    assert weekdays <= {0, 1, 2, 3, 4}


def test_merge_crontab_replaces_only_managed_block():
    """Installing must preserve unrelated cron entries and swap only our block."""
    managed = "# generated_at=now\n0 * * * * run-a\n"
    unrelated = "30 2 * * * /usr/bin/backup\n"
    merged_first = SchedulerService._merge_crontab(unrelated, managed)
    assert merged_first == f"{unrelated}{CRON_BLOCK_BEGIN}\n{managed}{CRON_BLOCK_END}\n"
    # Re-merging with new content replaces the old block, keeps the rest.
    managed_new = "# generated_at=later\n0 1 * * * run-b\n"
    merged_again = SchedulerService._merge_crontab(merged_first, managed_new)
    assert "run-a" not in merged_again
    assert "run-b" in merged_again
    assert "/usr/bin/backup" in merged_again


def test_merge_crontab_drops_block_when_no_entries():
    """With no enabled schedules the install must leave the crontab clean."""
    unrelated = "30 2 * * * /usr/bin/backup\n"
    stale = (
        f"{CRON_BLOCK_BEGIN}\n# header\n* * * * * run-a\n{CRON_BLOCK_END}\n"
    )
    # No entries in the managed file → block removed entirely.
    empty_managed = "# Managed by ... — do not edit.\n# generated_at=now\n"
    assert SchedulerService._merge_crontab(unrelated + stale, empty_managed) == unrelated
    # Even an empty user crontab ends up empty (no stray markers).
    assert SchedulerService._merge_crontab("", empty_managed) == ""
