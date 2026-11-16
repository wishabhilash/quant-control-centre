"""Runtime files: files the strategy itself wrote into its directory.

The editor lists them read-only (never logs, never the pid file, never
dot-hidden entries), and reading them is jailed to the strategy directory —
no traversal, no absolute paths, no symlinks pointing outside, no platform
resources. They are never part of the S3 project (golden rule 1).
"""
import pytest

from app.services.code_service import CodeService


@pytest.fixture
def code_service(store, settings):
    return CodeService(store=store, settings=settings)


@pytest.fixture
def strategy_dir(settings):
    """A strategy directory on disk with a mix of runtime and excluded files."""
    d = settings.strategies_dir_path() / "STR-001"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text('{"positions": 1}')
    sub = d / "data"
    sub.mkdir(exist_ok=True)
    (sub / "candles.csv").write_text("ts,open\n1,2\n")
    # Excluded: logs, pid, hidden — at top level and nested.
    (d / "strategy.log").write_text("log\n")
    (d / "runner.log").write_text("runner log\n")
    (d / "strategy.pid").write_text("123\n")
    (d / ".quant-managed").write_text("marker\n")
    (d / ".hidden").write_text("nope\n")
    (sub / ".secret").write_text("nope\n")
    (sub / "debug.log").write_text("nope\n")
    (d / ".venv").mkdir(exist_ok=True)
    (d / ".venv" / "share").mkdir(exist_ok=True)
    (d / ".venv" / "share" / "runtime_config.json").write_text("{}\n")
    return d


def test_lists_runtime_files_but_never_logs_or_hidden(code_service, strategy_dir):
    files = {f.path: f.size_bytes for f in code_service.list_runtime_files("STR-001")}
    # Visible: anything the strategy wrote, at any depth.
    assert "state.json" in files
    assert "data/candles.csv" in files
    # Excluded: logs, pid, hidden, .venv — at every level.
    assert "strategy.log" not in files
    assert "runner.log" not in files
    assert "strategy.pid" not in files
    assert ".quant-managed" not in files
    assert ".hidden" not in files
    assert "data/.secret" not in files
    assert "data/debug.log" not in files
    assert not any(p.startswith(".venv/") for p in files)


def test_list_is_empty_when_the_directory_does_not_exist(code_service, settings):
    assert code_service.list_runtime_files("STR-404") == []


def test_read_returns_text_for_a_nested_runtime_file(code_service, strategy_dir):
    out = code_service.read_runtime_file("STR-001", "data/candles.csv")
    assert out.encoding == "utf-8"
    assert not out.truncated
    assert out.content == "ts,open\n1,2\n"


def test_read_refuses_traversal_and_absolute_paths(code_service, strategy_dir):
    with pytest.raises(ValueError):
        code_service.read_runtime_file("STR-001", "../STR-002/state.json")
    with pytest.raises(ValueError):
        code_service.read_runtime_file("STR-001", "/etc/passwd")


def test_read_refuses_logs_pid_and_hidden(code_service, strategy_dir):
    for path in ("strategy.log", "runner.log", "strategy.pid",
                 ".quant-managed", ".hidden", "data/.secret", "data/debug.log"):
        with pytest.raises(ValueError):
            code_service.read_runtime_file("STR-001", path)


def test_read_refuses_a_symlink_escaping_the_strategy_dir(code_service, strategy_dir):
    outside = strategy_dir.parent / "outside.txt"
    outside.write_text("secret\n")
    link = strategy_dir / "leak.txt"
    try:
        link.symlink_to(outside)
    except OSError:  # pragma: no cover — platform without symlink support
        pytest.skip("symlinks not supported here")
    with pytest.raises(ValueError):
        code_service.read_runtime_file("STR-001", "leak.txt")


def test_read_binary_runtime_file_returns_base64(code_service, strategy_dir):
    payload = bytes(range(256)) * 4
    (strategy_dir / "blob.bin").write_bytes(payload)
    out = code_service.read_runtime_file("STR-001", "blob.bin")
    assert out.encoding == "base64"
    import base64
    assert base64.b64decode(out.content) == payload


def test_read_flags_truncation_over_the_cap(code_service, strategy_dir,
                                            monkeypatch):
    import app.services.code_service as cs

    monkeypatch.setattr(cs, "RUNTIME_MAX_READ_BYTES", 10)
    (strategy_dir / "big.txt").write_text("x" * 25)
    out = code_service.read_runtime_file("STR-001", "big.txt")
    assert out.truncated and len(out.content) == 10


def test_read_missing_file_is_a_file_not_found(code_service, strategy_dir):
    with pytest.raises(FileNotFoundError):
        code_service.read_runtime_file("STR-001", "nope.json")


def test_runtime_files_are_not_project_files(code_service, store,
                                             strategy_service, settings):
    """Golden rule 1: listing/reading runtime files never promotes them into
    the S3 project — the saved document is untouched by these endpoints."""
    from pathlib import Path

    from app.models.strategy import StrategyCreate

    doc = strategy_service.create(StrategyCreate(
        name="Runtime", description="d", files={"main.py": "print('hi')"}))
    sid = doc.strategy_id

    # Give the strategy a runtime file on disk, as a run would.
    d = settings.strategies_dir_path() / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text('{"positions": 1}\n')

    before = dict(store.get_document(sid).code.files)
    listed = code_service.list_runtime_files(sid)
    assert [f.path for f in listed] == ["state.json"]
    code_service.read_runtime_file(sid, "state.json")
    after = dict(store.get_document(sid).code.files)
    assert before == after  # nothing from disk leaked into the project
    assert "state.json" not in after
    assert set(after) == {"main.py"}
