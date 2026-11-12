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
