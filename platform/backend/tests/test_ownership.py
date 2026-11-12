"""Directory-ownership guard (strategies can never be silently overwritten).

The platform only ever writes into a strategies root / strategy directory it
created itself (``.quant-managed`` marker). A directory that exists without the
marker is treated as foreign content — a misconfigured ``STRATEGIES_DIR`` or a
reprovisioned server with disk leftovers under a reused ``STR-NNN`` id fails
fast instead of overwriting data.
"""
from pathlib import Path

import pytest

from app.models.strategy import StrategyCreate
from app.core.paths import (
    MANAGED_MARKER,
    UnownedDirectoryError,
    ensure_managed_dir,
    is_managed,
)


def test_empty_directory_is_adopted_and_marked(tmp_path):
    d = tmp_path / "strategies"
    ensure_managed_dir(d)
    assert is_managed(d)
    assert (d / MANAGED_MARKER).read_text().startswith("Created by")


def test_vcs_artefacts_are_adoptable(tmp_path):
    """A fresh checkout may contain only `.gitkeep` / `.DS_Store`."""
    d = tmp_path / "strategies"
    d.mkdir()
    (d / ".gitkeep").write_text("")
    ensure_managed_dir(d)
    assert is_managed(d)


def test_foreign_content_is_refused(tmp_path):
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "existing-strategy-0001").mkdir()
    (d / "existing-strategy-0001" / "main.py").write_text("print('live')\n")
    with pytest.raises(UnownedDirectoryError):
        ensure_managed_dir(d)
    # Nothing was touched.
    assert (d / "existing-strategy-0001" / "main.py").exists()
    assert not is_managed(d)


def test_marked_directory_passes(tmp_path):
    d = tmp_path / "strategies"
    d.mkdir()
    (d / MANAGED_MARKER).write_text("x\n")
    ensure_managed_dir(d)  # must not raise
    assert is_managed(d)


def test_process_service_refuses_unowned_root(strategy_service, monkeypatch, tmp_path):
    """The strategies root guard fails fast at service construction."""
    from app.services.process_service import ProcessService

    root = tmp_path / "strategies"
    root.mkdir()
    (root / "unrelated-data").mkdir()
    settings = strategy_service.settings.model_copy(
        update={"strategies_dir": root, "platform_root": tmp_path}
    )
    with pytest.raises(UnownedDirectoryError):
        ProcessService(settings, strategy_service)


def test_deploy_refuses_to_overwrite_unowned_strategy_dir(strategy_service, settings, monkeypatch):
    """Disk leftovers under a reused STR-NNN id are never adopted."""
    from app.models.strategy import StrategyCreate
    from app.services.deploy_service import DeployError, DeployService

    doc = strategy_service.create(StrategyCreate(name="New", files={"main.py": "print('hi')\n"}))
    target = settings.strategies_dir_path() / doc.strategy_id
    target.mkdir()
    (target / "old-broker-state.json").write_text("{}\n")

    service = DeployService(settings, strategy_service)
    with pytest.raises(DeployError, match="ownership marker"):
        service.deploy(doc.strategy_id)

    # The foreign content is untouched, and the strategy was never deployed.
    assert (target / "old-broker-state.json").exists()
    assert service.strategy_service.get(doc.strategy_id).deployment.status == "not_deployed"


def test_deploy_marks_created_strategy_dir(strategy_service, settings, monkeypatch):
    """A normal deploy creates the strategy dir as platform-owned."""
    from app.models.strategy import StrategyCreate
    from app.services.deploy_service import DeployService

    doc = strategy_service.create(StrategyCreate(name="Ok", files={"main.py": "print('hi')\n"}))

    def fake_run(cmd, *args, **kwargs):
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr("app.services.deploy_service.subprocess.run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3")

    service = DeployService(settings, strategy_service)
    service.deploy(doc.strategy_id)
    target = settings.strategies_dir_path() / doc.strategy_id
    assert is_managed(target)
    # And a deploy sync never deletes the marker.
    assert is_managed(target)


def test_delete_refuses_unowned_strategy_dir(strategy_service, settings):
    from app.services.process_service import ProcessService, ProcessServiceError

    doc = strategy_service.create(StrategyCreate(name="Ghost", files={"main.py": "print('x')\n"}))
    target = settings.strategies_dir_path() / doc.strategy_id
    target.mkdir()
    (target / "precious").write_text("data\n")

    proc = ProcessService(settings, strategy_service)
    with pytest.raises(ProcessServiceError, match="ownership marker"):
        proc.remove_local_resources(doc.strategy_id)
    assert (target / "precious").exists()
