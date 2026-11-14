"""The cron-fired runner's verdict must reach the dashboard.

``runner.log`` (the cron redirect) is not shown in the UI, so a skipped or
failed tick would be invisible. The runner therefore appends its verdict to the
strategy's own ``strategy.log`` — the single bounded log the Logs tab streams.
"""
import importlib.util
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("quant_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_writes_its_verdict_into_the_strategy_log(settings):
    module = _load_runner()
    module.log_to_strategy("STR-001", "SKIP: strategy STR-001 is disabled", settings=settings)
    log_file = settings.strategies_dir_path() / "STR-001" / "strategy.log"
    text = log_file.read_text()
    assert "SKIP: strategy STR-001 is disabled" in text
    assert "| RUNNER |" in text


def test_runner_logging_never_raises(settings):
    """A broken log target must never break the run itself."""
    module = _load_runner()
    # A strategy id with a path separator would escape the strategies dir —
    # the helper must swallow the error instead of failing the run.
    module.log_to_strategy("..", "x", settings=settings)  # must not raise


def test_runner_prints_and_logs_the_skip_reasons(settings, capsys, monkeypatch):
    """The disabled-strategy path prints AND logs the same verdict."""
    module = _load_runner()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    # Simulate the runner's own skip branch (without the S3 round trip).
    module.log_to_strategy("STR-002", "SKIP: strategy STR-002 is disabled", settings=settings)
    out = capsys.readouterr()
    assert out.out == ""  # the helper itself prints nothing
    text = (settings.strategies_dir_path() / "STR-002" / "strategy.log").read_text()
    assert "SKIP: strategy STR-002 is disabled" in text
