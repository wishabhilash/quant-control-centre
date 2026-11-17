"""Process service tests (spec 19).

The fixed entry point (``main.py``) is launched with a schedule's ``command``
split into an argument array — never a single interpolated shell string
(golden rule 6, no shell).
"""
from app.models.strategy import StrategyCreate
from app.services.process_service import ProcessService


def _deployed(strategy_service, settings) -> str:
    """Create a deployed strategy with its OWN venv interpreter (golden rule 3)."""
    doc = strategy_service.create(StrategyCreate(name="Proc", files={"main.py": "print(1)"}))
    doc = strategy_service.get(doc.strategy_id)
    doc.deployment.status = "deployed"
    strategy_service.store.update_document(doc)
    venv_bin = settings.strategies_dir_path() / doc.strategy_id / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "python").write_text("#!/bin/sh\n")
    return doc.strategy_id


def _capture_launch(monkeypatch):
    """Neutralise the real subprocess + the background timeout thread so the test
    never blocks; record every command the launcher would run."""
    calls = []

    def fake_popen(cmd, *args, **kwargs):
        calls.append(list(cmd))

        class _P:
            pid = 987654  # an unassigned pid; the thread is neutralised anyway

        return _P()

    class _NoopThread:
        def __init__(self, *a, **k):  # noqa: ARG002
            pass

        def start(self):
            return None

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("threading.Thread", _NoopThread)
    return calls


def test_launch_appends_command_as_arg_array(strategy_service, settings, monkeypatch):
    sid = _deployed(strategy_service, settings)
    calls = _capture_launch(monkeypatch)
    svc = ProcessService(settings, strategy_service)

    info = svc.start(sid, schedule_id="SCH-001", command="--mode backtest --date 2024-01-01")
    assert info.state == "running"

    assert calls, "expected the launcher to invoke the venv interpreter"
    cmd = calls[-1]
    # [interpreter, main.py, *args] — the fixed entry point plus the command.
    assert cmd[1].endswith("main.py"), cmd
    assert cmd[2:] == ["--mode", "backtest", "--date", "2024-01-01"], cmd
    # No shell: each argument is a discrete array element, not one interpolated
    # string (golden rule 6).
    assert "--mode backtest" not in cmd


def test_launch_without_command_passes_no_args(strategy_service, settings, monkeypatch):
    sid = _deployed(strategy_service, settings)
    calls = _capture_launch(monkeypatch)
    svc = ProcessService(settings, strategy_service)

    svc.start(sid, schedule_id=None)

    cmd = calls[-1]
    assert cmd[1].endswith("main.py"), cmd
    assert len(cmd) == 2, "interpreter + entry point only when no command is set"


# --- halting a live run -------------------------------------------------------
def _fast_grace(settings):
    """A 1 s grace period: the zombie edge (see the test below) means stop()
    waits out the whole grace period before its SIGKILL fallback — keep the
    suite fast without weakening what is asserted."""
    return settings.model_copy(update={"process_grace_period_seconds": 1})


def _running_subprocess(svc, sid) -> "subprocess.Popen":
    """A REAL sleeping process registered as the strategy's run — so the halt
    path exercises genuine signals, not mocks (the timeout thread is left out:
    stop() is the behaviour under test). start_new_session=True mirrors the
    launcher (process_service._launch): the child leads its OWN process group,
    so the killpg targets only it — never the test runner."""
    import subprocess as sp

    proc = sp.Popen(["python3", "-c", "import time; time.sleep(60)"],
                    start_new_session=True)
    svc._write_state(sid, {"pid": proc.pid, "state": "running"})
    return proc


def test_stop_terminates_a_live_run(strategy_service, settings):
    """stop() kills the run: the process dies and the state is marked stopped.

    The exact signal (SIGTERM vs the post-grace SIGKILL fallback) is an
    implementation detail here: a test-spawned child is a zombie until pytest
    reaps it, and a zombie still answers `os.kill(pid, 0)` — in production the
    run's parent (the runner) has exited, so this edge does not arise. The
    user-facing guarantee is simply: stopped is True, and the process is gone.
    """
    sid = _deployed(strategy_service, settings)
    svc = ProcessService(_fast_grace(settings), strategy_service)
    proc = _running_subprocess(svc, sid)

    result = svc.stop(sid, graceful=True)

    assert result["stopped"] is True
    proc.wait(timeout=10)  # reaped, and gone
    assert proc.returncode in (-15, -9, 15, 9)  # died by signal, not by itself
    assert svc.read_state(sid).get("state") == "stopped"


def test_stop_reports_when_not_running(strategy_service, settings):
    sid = _deployed(strategy_service, settings)
    svc = ProcessService(settings, strategy_service)
    result = svc.stop(sid)
    assert result == {"stopped": False, "reason": "not running"}


def test_status_heals_a_stale_running_record(strategy_service, settings):
    """A run that exited on its own leaves the state file saying ``running``
    (the runner is ephemeral — nothing marks the exit). The first status read
    must report STOPPED and heal the file — the UI chip polls this, so a
    crashed run must never show as running forever (regression: STR-004's
    chip said ● running for a process that was already dead)."""
    sid = _deployed(strategy_service, settings)
    svc = ProcessService(settings, strategy_service)
    dead_pid = _spawn_and_reap()  # a real, exited pid
    svc._write_state(sid, {"pid": dead_pid, "state": "running"})

    status = svc.runtime_status(sid)
    assert status.state == "stopped" and status.process is None
    healed = svc.read_state(sid)
    assert healed["state"] == "stopped" and "healed_at" in healed


def test_halt_on_a_stale_record_heals_and_answers_409(strategy_service,
                                                      settings, monkeypatch):
    """Halting a strategy whose run already died: 409 (nothing to kill) AND
    the stale ``running`` record is healed — the next poll shows idle."""
    import pytest
    from fastapi import HTTPException

    import app.api.deploy as deploy_api
    from app.api.deploy import halt

    monkeypatch.setattr(deploy_api, "_require_owner", lambda *a, **k: None)
    sid = _deployed(strategy_service, settings)
    svc = ProcessService(settings, strategy_service)
    dead_pid = _spawn_and_reap()
    svc._write_state(sid, {"pid": dead_pid, "state": "running"})

    with pytest.raises(HTTPException) as excinfo:
        halt(sid, principal=None, proc=svc, strategies=strategy_service)
    assert excinfo.value.status_code == 409
    assert svc.read_state(sid)["state"] == "stopped"


def _spawn_and_reap() -> int:
    """A pid that is definitively NOT alive: spawn a child, wait for it to
    exit, and return its pid (reaped — no zombie ambiguity)."""
    import subprocess as sp

    proc = sp.Popen(["true"])
    proc.wait()
    return proc.pid


def test_halt_route_kills_the_run_and_leaves_schedules_alone(strategy_service,
                                                             settings,
                                                             monkeypatch):
    """The halt endpoint is the user-facing kill path: it stops the live
    process, logs a HALT verdict into the strategy's own log, and —
    deliberately — never touches the schedules (that is `stop`'s job).
    Ownership itself is patched out here (covered by the auth tests); this
    test targets halt's semantics."""
    import inspect

    import pytest
    from fastapi import HTTPException

    import app.api.deploy as deploy_api
    from app.api.deploy import halt

    monkeypatch.setattr(deploy_api, "_require_owner", lambda *a, **k: None)

    sid = _deployed(strategy_service, settings)
    svc = ProcessService(_fast_grace(settings), strategy_service)

    # Not running → 409 (a distinct, visible failure — not a silent ok).
    with pytest.raises(HTTPException) as excinfo:
        halt(sid, principal=None, proc=svc, strategies=strategy_service)
    assert excinfo.value.status_code == 409

    proc = _running_subprocess(svc, sid)
    result = halt(sid, principal=None, proc=svc, strategies=strategy_service)
    assert result["stopped"] is True
    proc.wait(timeout=10)
    # The verdict is in the strategy's single log (golden rule 4)…
    log_text = (settings.strategies_dir_path() / sid / "strategy.log").read_text()
    assert "HALT" in log_text and "schedules were not changed" in log_text
    # …and the route never touches the scheduler (no reconcile call — stop's job).
    source = inspect.getsource(halt)
    assert "disable_strategy" not in source and "reconcile" not in source
