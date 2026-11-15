"""Scheduler enable/disable (schedule-only trigger model, spec 11 / 19).

A strategy is only ever run by its schedules via the generic runner; the
platform's start/stop operations enable/disable those schedules rather than
launching a process. The trigger is an in-process APScheduler
(``strategy_scheduler.StrategyScheduler``) — tests drive the reconcile logic
through a spy that records job registrations instead of starting a real
scheduler.
"""
from pathlib import Path

import pytest

from app.models.strategy import StrategyCreate, StrategyUpdate, ScheduleUpdate
from app.services import strategy_scheduler as ss
from app.services.scheduler_service import SchedulerService


class SpyScheduler:
    """Test double for StrategyScheduler: records job mutations."""

    def __init__(self):
        self.enabled = True
        self.started = False
        self.jobs = {}  # job_id -> (strategy_id, schedule)
        self.event_log = []

    def start(self):
        self.started = True

    def shutdown(self):
        self.started = False

    def upsert(self, strategy_id, schedule):
        jid = ss.job_id(strategy_id, schedule.schedule_id)
        self.jobs[jid] = (strategy_id, schedule)
        self.event_log.append(("upsert", jid, schedule.cron_expression,
                               schedule.timezone))

    def remove_job(self, jid):
        self.jobs.pop(jid, None)
        self.event_log.append(("remove", jid))

    def job_ids(self):
        return set(self.jobs)

    def next_run(self, jid):
        return None

    @property
    def job_store_path(self):
        return Path("/tmp/spy-job-store.sqlite3")


def _create(strategy_service):
    return strategy_service.create(
        StrategyCreate(name="Gamma", files={"main.py": "print(1)"})
    )


def _add_schedule(scheduler_service: SchedulerService, strategy_id: str) -> str:
    sched = scheduler_service.create_schedule(strategy_id, "hourly", "0 * * * *", "Asia/Kolkata", True)
    return sched.schedule_id


@pytest.fixture
def live(scheduler_service, settings, monkeypatch):
    """The scheduler service wired to a SpyScheduler, dry-run off.

    The module-level ``shared_scheduler`` is patched too, so the SchedulerService
    instances the strategy service constructs internally (update/delete paths)
    drive the *same* spy — exactly the shared-singleton guarantee production has.
    """
    spy = SpyScheduler()
    monkeypatch.setattr(ss, "shared_scheduler", lambda: spy)
    settings.scheduler_dry_run = False
    scheduler_service.strategy_scheduler = spy
    return scheduler_service, spy


def test_enable_strategy_registers_jobs(live):
    svc, spy = live
    doc = _create(svc.strategy_service)
    sid = _add_schedule(svc, doc.strategy_id)

    # Start a strategy = register its schedule jobs.
    res = svc.enable_strategy(doc.strategy_id)
    assert res["enabled"] is True
    assert res["mode"] == "apscheduler"
    assert res["jobs"] == 1
    jid = ss.job_id(doc.strategy_id, sid)
    assert jid in spy.jobs
    assert spy.started is True
    assert spy.event_log[-1] == ("upsert", jid, "0 * * * *", "Asia/Kolkata")
    # The stored document is now enabled.
    assert svc.strategy_service.get(doc.strategy_id).enabled is True


def test_disable_strategy_removes_jobs(live):
    svc, spy = live
    doc = _create(svc.strategy_service)
    sid = _add_schedule(svc, doc.strategy_id)
    svc.enable_strategy(doc.strategy_id)
    jid = ss.job_id(doc.strategy_id, sid)
    assert jid in spy.jobs

    # Stop a strategy = deregister its jobs; the runner will no longer trigger it.
    res = svc.disable_strategy(doc.strategy_id)
    assert res["enabled"] is False
    assert jid not in spy.jobs
    assert ("remove", jid) in spy.event_log
    assert svc.strategy_service.get(doc.strategy_id).enabled is False


def test_restart_reconcile_rebuilds_jobs_from_s3(live):
    """The startup reconcile diffs the job store against S3: stale jobs go."""
    svc, spy = live
    doc = _create(svc.strategy_service)
    sid = _add_schedule(svc, doc.strategy_id)
    # Simulate a stale job left in the store from before a restart (the
    # schedule has since been disabled in S3).
    stale_id = ss.job_id(doc.strategy_id, "SCH-999")
    spy.jobs[stale_id] = (doc.strategy_id, None)

    svc.reconcile()  # full sync, as main.py's startup does

    assert stale_id not in spy.jobs
    assert ss.job_id(doc.strategy_id, sid) in spy.jobs


def test_update_enabled_reconciles_jobs(live):
    svc, spy = live
    doc = _create(svc.strategy_service)
    sid = _add_schedule(svc, doc.strategy_id)
    svc.enable_strategy(doc.strategy_id)
    jid = ss.job_id(doc.strategy_id, sid)
    assert jid in spy.jobs

    # Toggling enabled via update() must reconcile the scheduler too
    # (strategy_service constructs its own SchedulerService internally — the
    # shared scheduler guarantee makes it drive the same spy).
    svc.strategy_service.update(doc.strategy_id, StrategyUpdate(enabled=False))
    assert jid not in spy.jobs
    svc.strategy_service.update(doc.strategy_id, StrategyUpdate(enabled=True))
    assert jid in spy.jobs


def test_reconcile_is_scoped_to_one_strategy(live):
    """A scoped reconcile (schedule CRUD on strategy A) never touches B's jobs."""
    svc, spy = live
    doc_a = _create(svc.strategy_service)
    doc_b = _create(svc.strategy_service)
    sid_a = _add_schedule(svc, doc_a.strategy_id)
    sid_b = _add_schedule(svc, doc_b.strategy_id)
    svc.reconcile()  # full: both registered
    jid_a, jid_b = ss.job_id(doc_a.strategy_id, sid_a), ss.job_id(doc_b.strategy_id, sid_b)
    assert jid_a in spy.jobs and jid_b in spy.jobs

    svc.update_schedule(doc_a.strategy_id, sid_a, ScheduleUpdate(enabled=False))
    svc.reconcile(doc_a.strategy_id)  # what the schedules API does after an update
    assert jid_a not in spy.jobs
    assert jid_b in spy.jobs  # untouched


def test_full_reconcile_drops_jobs_of_deleted_schedules(live):
    svc, spy = live
    doc = _create(svc.strategy_service)
    sid = _add_schedule(svc, doc.strategy_id)
    svc.reconcile()
    assert ss.job_id(doc.strategy_id, sid) in spy.jobs

    svc.delete_schedule(doc.strategy_id, sid)
    svc.reconcile()
    assert ss.job_id(doc.strategy_id, sid) not in spy.jobs
    assert spy.jobs == {}


def test_dry_run_touches_nothing(scheduler_service):
    """SCHEDULER_DRY_RUN (the local/test default) registers no jobs at all."""
    spy = SpyScheduler()
    scheduler_service.strategy_scheduler = spy
    doc = _create(scheduler_service.strategy_service)
    _add_schedule(scheduler_service, doc.strategy_id)

    res = scheduler_service.reconcile()
    assert res["mode"] == "dry_run"
    assert res["dry_run"] is True
    assert res["jobs"] == 1  # what *would* be scheduled
    assert spy.jobs == {} and spy.started is False


def test_invalid_cron_is_skipped_not_fatal(live):
    """One unparseable expression must not block the other schedules."""
    svc, spy = live
    doc = _create(svc.strategy_service)
    good = svc.create_schedule(doc.strategy_id, "ok", "0 * * * *", "Asia/Kolkata", True)
    bad = svc.create_schedule(doc.strategy_id, "broken", "99 99 * * *", "Asia/Kolkata", True)

    res = svc.reconcile(doc.strategy_id)
    assert ss.job_id(doc.strategy_id, good.schedule_id) in spy.jobs
    assert ss.job_id(doc.strategy_id, bad.schedule_id) not in spy.jobs
    assert res["jobs"] == 1
    assert [s["schedule_id"] for s in res["skipped"]] == [bad.schedule_id]


def test_reconcile_precreates_the_strategy_dir(live):
    """The job body redirects into the strategy's runner.log — the dir must exist."""
    svc, spy = live
    doc = _create(svc.strategy_service)
    _add_schedule(svc, doc.strategy_id)
    svc.reconcile(doc.strategy_id)
    assert (svc.settings.strategies_dir_path() / doc.strategy_id).is_dir()


def test_runner_paths_are_settings_derived(scheduler_service):
    """The scheduled runner paths follow PLATFORM_ROOT, not /opt/quant."""
    python_path, runner_path = scheduler_service.runner_paths()
    root = str(scheduler_service.settings.resolved_platform_root())
    assert root in python_path and root in runner_path
    assert "/opt/quant" not in python_path
    assert runner_path.endswith("scripts/runner.py")


def test_job_ids_round_trip():
    assert ss.job_id("STR-001", "SCH-012") == "strategy-STR-001-SCH-012"
    assert ss.parse_job_id("strategy-STR-001-SCH-012") == ("STR-001", "SCH-012")
    # Foreign job ids (other job stores share the scheduler family) parse to None.
    assert ss.parse_job_id("dhan-token-USR-1") is None
    assert ss.parse_job_id("strategy-not-an-id") is None


def test_cron_trigger_maps_five_fields():
    """5-field cron: no seconds field — `0 9 * * 1-5` fires 09:00 Mon–Fri."""
    import datetime as _dt
    import zoneinfo

    t = ss.cron_trigger("0 9 * * 1-5", "Asia/Kolkata")
    base = _dt.datetime(2026, 11, 15, 10, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Kolkata"))
    nxt = t.get_next_fire_time(None, base)  # a Sunday → next is Monday 09:00
    assert (nxt.hour, nxt.minute, nxt.second) == (9, 0, 0)
    assert nxt.weekday() == 0


def test_cron_day_of_week_numbering_matches_cron():
    """cron numbers days 0=Sunday; APScheduler numbers them 0=Monday.

    Passing the raw cron field through would fire "1-5" (Mon–Fri) Tue–Sat —
    the conversion must shift the numbering (and accept 7=Sunday).
    """
    import datetime as _dt
    import zoneinfo

    base = _dt.datetime(2026, 11, 15, 10, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Kolkata"))

    # cron `1-5` = Mon–Fri: from a Sunday the next fire is Monday.
    nxt = ss.cron_trigger("0 9 * * 1-5", "Asia/Kolkata").get_next_fire_time(None, base)
    assert nxt.weekday() == 0

    # cron `0` (and its 7 alias) = Sunday: from Sunday 10:00, next is next Sunday.
    for dow in ("0", "7"):
        nxt = ss.cron_trigger(f"0 9 * * {dow}", "Asia/Kolkata").get_next_fire_time(None, base)
        assert nxt.weekday() == 6

    # `*` must survive as every day: next fire is the very next day (Monday).
    nxt = ss.cron_trigger("0 9 * * *", "Asia/Kolkata").get_next_fire_time(None, base)
    assert nxt.weekday() == 0


def test_cron_trigger_maps_six_fields_with_seconds():
    """6-field cron: the FIRST field is seconds (same rule croniter applies)."""
    import datetime as _dt
    import zoneinfo

    t = ss.cron_trigger("30 20 9 * * 1-5", "Asia/Kolkata")
    base = _dt.datetime(2026, 11, 15, 10, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Kolkata"))
    nxt = t.get_next_fire_time(None, base)
    assert (nxt.hour, nxt.minute, nxt.second) == (9, 20, 30)
    assert nxt.weekday() == 0


def test_cron_trigger_rejects_bad_expressions():
    import pytest

    for bad in ("0 9 * *", "not a cron", "99 99 * * *"):
        with pytest.raises(ValueError):
            ss.cron_trigger(bad, "Asia/Kolkata")
    with pytest.raises(ValueError):
        ss.cron_trigger("0 * * * *", "Not/AZone")


def test_run_schedule_invokes_the_runner(settings, monkeypatch, tmp_path):
    """The job body runs the generic runner with both ids, output to runner.log."""
    monkeypatch.setattr(ss, "get_settings", lambda: settings)
    monkeypatch.setattr(ss, "runner_paths", lambda s: ("/venv/py", "/backend/runner.py"))
    calls = {}

    def fake_run(cmd, stdout=None, stderr=None):
        calls["cmd"] = cmd
        calls["stdout"] = stdout
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    ss.run_schedule("STR-001", "SCH-003")
    assert calls["cmd"] == ["/venv/py", "/backend/runner.py",
                            "--strategy-id", "STR-001", "--schedule-id", "SCH-003"]
    # The runner's output is appended to the strategy's own runner.log.
    assert calls["stdout"].name == str(
        settings.strategies_dir_path() / "STR-001" / "runner.log")


def test_run_schedule_survives_a_foreign_strategy_dir(settings, monkeypatch):
    """A dir the platform does not own must not crash the job (it logs, returns)."""
    monkeypatch.setattr(ss, "get_settings", lambda: settings)
    root = settings.strategies_dir_path()
    foreign = root / "STR-666"
    foreign.mkdir(parents=True)
    (foreign / ".quant-managed").unlink(missing_ok=True)
    (foreign / "keep.txt").write_text("not mine")

    monkeypatch.setattr(ss, "runner_paths", lambda s: ("/venv/py", "/backend/runner.py"))
    ran = {}
    monkeypatch.setattr(ss.subprocess, "run",
                        lambda *a, **k: ran.setdefault("called", True))

    ss.run_schedule("STR-666", "SCH-001")
    assert ran == {}  # the runner was never invoked
    assert (foreign / "keep.txt").exists()  # foreign content untouched


def test_strategy_scheduler_is_disabled_in_dry_run(settings):
    sched = ss.StrategyScheduler(settings)
    assert sched.enabled is False
    sched.start()  # must be a no-op, not start a real BackgroundScheduler
    assert sched.job_ids() == set()
    assert sched.job_store_path.name == "strategy_scheduler.sqlite3"


def test_strategy_scheduler_enabled_when_not_dry_run(settings):
    settings.scheduler_dry_run = False
    sched = ss.StrategyScheduler(settings)
    assert sched.enabled is False  # still off: the test environment never fires
    settings.environment = "development"
    assert sched.enabled is True


def test_scheduled_overview_parses_job_ids(live):
    svc, spy = live
    doc = _create(svc.strategy_service)
    sid = _add_schedule(svc, doc.strategy_id)
    spy.jobs[ss.job_id(doc.strategy_id, sid)] = (doc.strategy_id, None)
    spy.jobs["dhan-token-USR-1"] = ("USR-1", None)  # foreign job id: ignored

    rows = svc.scheduled_overview()
    assert len(rows) == 1
    assert rows[0]["strategy_id"] == doc.strategy_id
    assert rows[0]["schedule_id"] == sid
    assert rows[0]["next_run"] is None


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


def test_upcoming_runs_preview(scheduler_service):
    """The editor previews the upcoming runs (like EventBridge's next occurrences)."""
    runs = scheduler_service.upcoming("*/5 * * * *", "Asia/Kolkata", 5)
    assert len(runs) == 5
    # ISO timestamps, in the requested timezone.
    assert runs[0].startswith("20")
    assert "+05:30" in runs[0]


def test_upcoming_runs_rejects_invalid_cron(scheduler_service):
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
