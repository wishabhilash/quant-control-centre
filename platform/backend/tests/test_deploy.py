"""Deployment service tests (spec sections 10, 19).

Verifies that deploying a strategy creates its OWN ``.venv`` and installs the
requirements manifest into it (spec section 10 steps 8/9; golden rule 3 — an
independent Python venv per strategy). Subprocess is mocked so the test needs
neither a real interpreter nor network access.
"""
import shutil
from pathlib import Path

from app.models.strategy import StrategyCreate


def _fake_run_factory(calls):
    """A stand-in for ``subprocess.run``.

    Records every command. For ``python -m venv <dir>`` it materialises the
    venv layout (``bin/python`` + ``bin/pip``) so the later install / smoke
    steps find them; every other command (pip install, smoke import) just
    succeeds.
    """

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "venv" in cmd:
            venv_dir = Path(cmd[cmd.index("venv") + 1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "python").write_text("#!/bin/sh\n")
            (bin_dir / "pip").write_text("#!/bin/sh\n")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    return fake_run


def _failing_pip_run_factory(calls):
    """Like ``_fake_run_factory`` but the ``pip install`` step fails, to exercise
    the deploy error path (which must record the failure, not crash)."""
    import subprocess as _sp

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "venv" in cmd:
            venv_dir = Path(cmd[cmd.index("venv") + 1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "python").write_text("#!/bin/sh\n")
            (bin_dir / "pip").write_text("#!/bin/sh\n")
            return _sp.CompletedProcess(cmd, 0)
        if "install" in cmd:
            raise _sp.CalledProcessError(1, cmd, stderr="mock: dependency installation failed")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    return fake_run


def _deploy(strategy_service, settings, monkeypatch, files, run=None):
    from app.services.deploy_service import DeployService

    doc = strategy_service.create(StrategyCreate(name="Alpha", files=files))
    calls: list = []
    monkeypatch.setattr("app.services.deploy_service.subprocess.run", run or _fake_run_factory(calls))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3")
    svc = DeployService(settings, strategy_service)
    result = svc.deploy(doc.strategy_id)
    return svc, doc, result, calls


def test_deploy_creates_venv_and_installs_requirements(strategy_service, settings, monkeypatch):
    svc, doc, result, calls = _deploy(
        strategy_service,
        settings,
        monkeypatch,
        {"main.py": "print('hi')\n", "requirements.txt": "requests==2.32.0\n"},
    )

    # A per-strategy .venv was created (its own interpreter — golden rule 3).
    venv_python = svc.strategy_dir(doc.strategy_id) / ".venv" / "bin" / "python"
    assert venv_python.exists(), "deploy did not create a .venv for the strategy"

    # The requirements manifest was installed into that venv via pip -r.
    # pip is driven through the venv's own interpreter ("python -m pip"), so the
    # packages cannot land in the base environment or in ~/.local.
    pip_calls = [c for c in calls
                 if "-m" in c and "pip" in c and "install" in c
                 and "requirements.txt" in c[-1]]
    assert pip_calls, "expected a `pip install -r requirements.txt` call"
    assert pip_calls[-1][0].endswith("/.venv/bin/python"), pip_calls[-1]

    # And the strategy is reported deployed.
    assert result["status"] == "deployed"


def test_deploy_without_manifest_creates_venv_but_skips_pip(strategy_service, settings, monkeypatch):
    # No requirements file -> the venv is still created, but no pip install runs.
    svc, doc, result, calls = _deploy(
        strategy_service,
        settings,
        monkeypatch,
        {"main.py": "print('hi')\n"},
    )

    venv_python = svc.strategy_dir(doc.strategy_id) / ".venv" / "bin" / "python"
    assert venv_python.exists(), "venv should still be created without a manifest"
    assert not [c for c in calls if "install" in c], "no pip install should run without a manifest"
    assert result["status"] == "deployed"


def test_failed_deploy_marks_strategy_failed(strategy_service, settings, monkeypatch):
    """A failing ``pip install`` must raise ``DeployError`` AND persist the
    deployment status as ``failed`` — the failure recorder must exist and must
    not crash (regression: ``_record_failure`` was previously undefined, so every
    failed deploy blew up with ``AttributeError`` instead of surfacing cleanly).
    """
    import pytest

    from app.services.deploy_service import DeployError, DeployService


    doc = strategy_service.create(StrategyCreate(
        name="Omega",
        files={"main.py": "print('hi')\n", "requirements.txt": "nonexistent-pkg==9.9.9\n"},
    ))
    calls: list = []
    monkeypatch.setattr("app.services.deploy_service.subprocess.run", _failing_pip_run_factory(calls))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3")

    service = DeployService(settings, strategy_service)
    with pytest.raises(DeployError):
        service.deploy(doc.strategy_id)

    # The failure was recorded (status persisted), not masked by a crash.
    assert service.strategy_service.get(doc.strategy_id).deployment.status == "failed"


def test_deploy_writes_progress_into_strategy_log(strategy_service, settings, monkeypatch):
    """The strategy's own strategy.log records the deploy trail — staged files,
    venv, pip output, smoke test — so the Logs tab shows what a deploy did."""
    svc, doc, result, calls = _deploy(
        strategy_service,
        settings,
        monkeypatch,
        {"main.py": "print('hi')\n", "requirements.txt": "requests==2.32.0\n"},
    )

    log_text = (svc.strategy_dir(doc.strategy_id) / "strategy.log").read_text()
    assert "| DEPLOY | deploy started" in log_text
    assert "| DEPLOY | staged 2 file(s)" in log_text
    assert "| DEPLOY | installing dependencies from requirements.txt" in log_text
    assert "| DEPLOY | dependencies installed" in log_text
    assert "| DEPLOY | smoke test passed" in log_text
    assert "| DEPLOY | deployed (source hash" in log_text
    # No lines were lost to the platform logger only.
    assert "crontab reconciled" in log_text


def test_pip_failure_summary_shows_the_real_cause():
    """The deploy error must show WHY pip failed, not just pip's cache warning."""
    from app.services.deploy_service import _pip_failure_summary

    pip_output = (
        "WARNING: The directory '/opt/quant-control-centre/.cache/pip' or its parent "
        "directory is not owned or is not writable by the current user. The cache has "
        "been disabled. Check the permissions and owner of that directory.\n"
        "Collecting pandas==3.0.5\n"
        "  Preparing metadata (setup.py): started\n"
        "  error: command 'gcc' failed with exit code 1\n"
        "ERROR: Could not build wheels for pandas==3.0.5\n"
    )
    summary = _pip_failure_summary(pip_output)
    # The cache warning is noise and must not hide the real error.
    assert "The cache has been disabled" not in summary
    assert "Could not build wheels for pandas==3.0.5" in summary
    assert "gcc" in summary


def test_pip_failure_summary_handles_empty_output():
    from app.services.deploy_service import _pip_failure_summary

    assert "strategy log" in _pip_failure_summary("")


def test_pip_failure_summary_flags_a_source_build():
    """A wheel-less dependency is the most common cause — spell it out."""
    from app.services.deploy_service import _pip_failure_summary

    summary = _pip_failure_summary(
        "Building wheels for collected packages: aiohttp\n"
        "  Building wheel for aiohttp (pyproject.toml): started\n"
    )
    assert "no wheel for this Python version" in summary


def test_dependency_install_timeout_is_reported(strategy_service, settings, monkeypatch):
    """A hung source build must fail with a clear message, not hang forever."""
    import subprocess as _sp

    import pytest

    from app.models.strategy import StrategyCreate
    from app.services.deploy_service import DeployError, DeployService

    doc = strategy_service.create(StrategyCreate(
        name="Slow", files={"main.py": "print('hi')\n", "requirements.txt": "slowpkg==1.0\n"},
    ))

    def fake_run(cmd, *args, **kwargs):
        if "venv" in cmd:
            venv_dir = Path(cmd[cmd.index("venv") + 1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "python").write_text("#!/bin/sh\n")
            (bin_dir / "pip").write_text("#!/bin/sh\n")
            return _sp.CompletedProcess(cmd, 0)
        if "pip" in cmd:
            raise _sp.TimeoutExpired(cmd, kwargs.get("timeout", 1), output="Building wheel …")
        return _sp.CompletedProcess(cmd, 0)

    monkeypatch.setattr("app.services.deploy_service.subprocess.run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3")

    service = DeployService(settings, strategy_service)
    with pytest.raises(DeployError, match="timed out"):
        service.deploy(doc.strategy_id)


def test_venv_with_wrong_python_version_is_rebuilt(strategy_service, settings, monkeypatch):
    """A venv built with another Python must not be reused silently."""
    import subprocess as _sp

    from app.models.strategy import StrategyCreate
    from app.services.deploy_service import DeployService

    doc = strategy_service.create(StrategyCreate(
        name="Old", files={"main.py": "print('hi')\n"},
    ))
    # A venv left over from a Python 3.14 build (the strategy dir itself is
    # created through the ownership-aware helper).
    from app.core.paths import ensure_managed_dir
    target = ensure_managed_dir(settings.strategies_dir_path() / doc.strategy_id)
    venv_python = target / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("#!/bin/sh\necho 'Python 3.14.0'\n")

    def fake_run(cmd, *args, **kwargs):
        # The venv version probe must report 3.14 so the rebuild is triggered.
        if "--version" in cmd:
            return _sp.CompletedProcess(cmd, 0, stdout="Python 3.14.0")
        if "venv" in cmd:
            venv_dir = Path(cmd[cmd.index("venv") + 1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "python").write_text("#!/bin/sh\n")
            (bin_dir / "pip").write_text("#!/bin/sh\n")
            return _sp.CompletedProcess(cmd, 0)
        return _sp.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr("app.services.deploy_service.subprocess.run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3")

    service = DeployService(settings, strategy_service)
    service.deploy(doc.strategy_id)

    log_text = (service.strategy_dir(doc.strategy_id) / "strategy.log").read_text()
    assert "rebuilding the venv" in log_text
    assert "requires 3.12" in log_text


def test_smoke_test_failure_blocks_deploy(strategy_service, settings, monkeypatch):
    """An entry point that cannot be imported is a broken deployment: the deploy
    must fail (not silently mark ``deployed``) and record the error in
    strategy.log."""
    import pytest

    from app.services.deploy_service import DeployError, DeployService

    doc = strategy_service.create(StrategyCreate(
        name="Broken",
        files={"main.py": "import redis\nprint('hi')\n",
               "requirements.txt": "dhanhq==2.2.0\n"},
    ))

    import subprocess as _sp

    def fake_run(cmd, *args, **kwargs):
        if "venv" in cmd:
            venv_dir = Path(cmd[cmd.index("venv") + 1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "python").write_text("#!/bin/sh\n")
            (bin_dir / "pip").write_text("#!/bin/sh\n")
            return _sp.CompletedProcess(cmd, 0)
        if "install" in cmd:
            return _sp.CompletedProcess(cmd, 0, stdout="installed dhanhq\n")
        # The smoke import: simulates a missing module (redis not installed).
        return _sp.CompletedProcess(cmd, 1,
                                    stderr="ModuleNotFoundError: No module named 'redis'")

    calls: list = []
    monkeypatch.setattr("app.services.deploy_service.subprocess.run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3")

    service = DeployService(settings, strategy_service)
    with pytest.raises(DeployError, match="smoke test failed"):
        service.deploy(doc.strategy_id)

    # Status is failed, and the trail is visible in the strategy's own log.
    assert service.strategy_service.get(doc.strategy_id).deployment.status == "failed"
    log_text = (service.strategy_dir(doc.strategy_id) / "strategy.log").read_text()
    assert "smoke import returned 1" in log_text
    assert "ModuleNotFoundError" in log_text
