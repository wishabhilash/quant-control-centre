"""Scheduler enable/disable (cron-only trigger model, spec 11 / 19).

A strategy is only ever run by its cron via the generic runner; the platform's
start/stop operations enable/disable those crons rather than launching a process.
"""
from app.models.strategy import StrategyCreate, StrategyUpdate
from app.services.scheduler_service import SchedulerService


def _create(strategy_service):
    return strategy_service.create(
        StrategyCreate(name="Gamma", entrypoint="main.py", files={"main.py": "print(1)"})
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
