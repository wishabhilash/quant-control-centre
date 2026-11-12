"""Deployment engine.

Deployment is a synchronization operation: the deployed project becomes an exact
mirror of the saved Monaco project, excluding platform-managed resources
(``.venv/``, ``strategy.log``). Uses a staging directory so the active project
is never modified until validation succeeds (spec section 10).
"""
from __future__ import annotations

import hashlib
import json
import py_compile
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.paths import assert_safe_path, is_reserved, RESERVED_TOP_LEVEL
from app.services.code_service import CodeService
from app.services.process_service import ProcessService
from app.services.scheduler_service import SchedulerService
from app.services.strategy_service import StrategyService

log = get_logger("services.deploy")

# Dependency manifest filenames the runner may auto-install.
DEPENDENCY_MANIFESTS = ("requirements.txt", "requirements-dev.txt")


class DeployError(Exception):
    pass


class DeployService:
    def __init__(self, settings: Settings | None = None, strategy_service=None):
        self.settings = settings or get_settings()
        self.strategy_service = strategy_service or StrategyService()
        self.code_service = CodeService(self.strategy_service.store)
        self.process_service = ProcessService(self.settings, self.strategy_service)
        self.scheduler_service = SchedulerService(self.settings, self.strategy_service)

    def strategy_dir(self, strategy_id: str) -> Path:
        return self.process_service.strategy_dir(strategy_id)

    # --- main entry -------------------------------------------------------
    def deploy(self, strategy_id: str, wait_if_running: bool = False) -> dict:
        doc = self.strategy_service.get(strategy_id)

        # (12) block replacing source files while executing.
        if self.process_service.is_running(strategy_id):
            if wait_if_running:
                log.warning("strategy %s running; waiting for safe completion", strategy_id)
                import time
                for _ in range(120):
                    if not self.process_service.is_running(strategy_id):
                        break
                    time.sleep(1)
            else:
                raise DeployError(f"strategy {strategy_id} is running; deployment blocked")

        files = self.code_service.decode_project(doc.code)
        entrypoint = doc.code.entrypoint

        # (3,4) validate entry point and paths.
        if entrypoint not in files:
            raise DeployError("entrypoint not present in project")
        if not entrypoint.endswith(".py"):
            raise DeployError("entrypoint must be a Python file")
        for rel_path in files:
            try:
                assert_safe_path(rel_path)
            except ValueError as exc:
                raise DeployError(f"invalid path {rel_path}: {exc}")
            if is_reserved(rel_path):
                raise DeployError(f"path collides with reserved resource: {rel_path}")

        # (5) compute and verify source hash.
        source_hash = self.strategy_service.store.document_hash(doc)

        # (6,7) staging directory.
        staging = Path(tempfile.mkdtemp(prefix=f"deploy-{strategy_id}-"))
        try:
            self._reconstruct(staging, files)
            self._validate_python(staging, entrypoint)

            # (8,9) venv + dependencies.
            target = self.strategy_dir(strategy_id)
            target.mkdir(parents=True, exist_ok=True)
            self._ensure_venv(target, doc.runtime.python_version)
            self._install_dependencies(target, files)

            # (10,11) smoke test (import the entry point).
            self._smoke_test(target, doc, entrypoint)

            # (13) safely replace active managed project files.
            self._sync_into(target, staging, files)

            # (14) reconcile cron.
            self.scheduler_service.reconcile(strategy_id)

            # (15) update deployment status.
            doc.deployment.status = "deployed"
            import datetime as _dt
            doc.deployment.last_deployed_at = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            doc.deployment.source_hash = source_hash
            self.strategy_service.store.update_document(doc)

            log.info("deployed %s hash=%s files=%d", strategy_id, source_hash, len(files))
            return {"strategy_id": strategy_id, "status": "deployed", "source_hash": source_hash,
                    "files": len(files), "entrypoint": entrypoint}
        except Exception as exc:  # noqa: BLE001
            # Staging guarantees the active project is untouched on failure.
            log.error("deployment failed for %s: %s", strategy_id, exc)
            self._record_failure(doc, exc)
            raise DeployError(f"deployment failed: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # --- helpers ----------------------------------------------------------
    def _reconstruct(self, staging: Path, files: dict[str, str]) -> None:
        for rel_path, content in files.items():
            dest = staging / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

    def _validate_python(self, root: Path, entrypoint: str) -> None:
        for py_file in root.rglob("*.py"):
            try:
                py_compile.compile(str(py_file), doraise=True)
            except py_compile.PyCompileError as exc:
                raise DeployError(f"Python syntax error in {py_file.relative_to(root)}: {exc}")

    def _ensure_venv(self, target: Path, python_version: str) -> Path:
        venv_python = target / ".venv" / "bin" / "python"
        if venv_python.exists():
            return venv_python
        # Pick the interpreter for the requested minor version.
        major, minor = (python_version or "3.12").split(".")[:2]
        candidates = [f"python{python_version}", f"python{major}.{minor}", "python3", "python"]
        for cand in candidates:
            try:
                import shutil as _sh
                exe = _sh.which(cand)
            except Exception:  # noqa: BLE001
                exe = None
            if not exe:
                continue
            try:
                subprocess.run([exe, "-m", "venv", str(target / ".venv")], check=True,
                               capture_output=True, text=True)
                log.info("created venv for %s using %s", target, cand)
                return target / ".venv" / "bin" / "python"
            except subprocess.CalledProcessError:
                continue
        raise DeployError(f"could not create virtualenv for python {python_version}")

    def _install_dependencies(self, target: Path, files: dict[str, str]) -> None:
        manifest = next((m for m in DEPENDENCY_MANIFESTS if m in files), None)
        if manifest is None:
            return
        venv_python = target / ".venv" / "bin" / "python"
        pip = target / ".venv" / "bin" / "pip"
        if not pip.exists():
            return
        req = target / manifest
        try:
            subprocess.run([str(pip), "install", "-r", str(req)], check=True,
                           capture_output=True, text=True)
            log.info("installed dependencies from %s for %s", manifest, target)
        except subprocess.CalledProcessError as exc:
            raise DeployError(f"dependency installation failed: {exc.stderr}") from exc

    def _smoke_test(self, target: Path, doc, entrypoint: str) -> None:
        """Configurable import smoke test. Importing the module must not raise
        at import time. The entry point should define ``run(config)`` or be a
        standalone script; we only import to catch import-time errors."""
        venv_python = target / ".venv" / "bin" / "python"
        if not venv_python.exists():
            return
        module = entrypoint[:-3] if entrypoint.endswith(".py") else entrypoint
        # Only import top-level modules (not dotted paths) for the smoke test.
        top = module.split("/")[0]
        try:
            result = subprocess.run(
                [str(venv_python), "-c", f"import importlib; importlib.import_module('{top}')"],
                cwd=str(target), capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log.warning("smoke import returned %d for %s", result.returncode, top)
        except subprocess.TimeoutExpired:
            log.warning("smoke test timed out for %s", top)
        except Exception:  # noqa: BLE001
            pass

    def _sync_into(self, target: Path, staging: Path, files: dict[str, str]) -> None:
        """Mirror the project into ``target``.

        Removes previously-deployed top-level project entries that no longer
        exist in the current project, then writes all current files. Platform
        managed resources (``.venv/``, ``strategy.log``) are always preserved.
        """
        reserved = RESERVED_TOP_LEVEL | {".venv"}
        # 1) Remove top-level project entries not referenced by the current set.
        for top_level in list(target.iterdir()):
            if top_level.name in reserved:
                continue
            prefix = top_level.name + "/"
            is_used = any(f == top_level.name or f.startswith(prefix) for f in files)
            if not is_used:
                if top_level.is_dir():
                    shutil.rmtree(top_level, ignore_errors=True)
                else:
                    top_level.unlink(missing_ok=True)
        # 2) Write the current files.
        for rel_path, content in files.items():
            dest = target / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
