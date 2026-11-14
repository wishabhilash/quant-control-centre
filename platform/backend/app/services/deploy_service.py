"""Deployment engine.

Deployment is a synchronization operation: the deployed project becomes an exact
mirror of the saved Monaco project, excluding platform-managed resources
(``.venv/``, ``strategy.log``). Uses a staging directory so the active project
is never modified until validation succeeds (spec section 10).
"""
from __future__ import annotations

import hashlib
import json
import os
import py_compile
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.paths import (
    MANAGED_MARKER,
    RESERVED_TOP_LEVEL,
    UnownedDirectoryError,
    assert_safe_path,
    ensure_managed_dir,
    is_managed,
    is_reserved,
)
from app.models.strategy import DEFAULT_ENTRYPOINT
from app.services.code_service import CodeService
from app.services.process_service import ProcessService
from app.services.scheduler_service import SchedulerService
from app.services.strategy_service import StrategyService

log = get_logger("services.deploy")

# Dependency manifest filenames the runner may auto-install.
DEPENDENCY_MANIFESTS = ("requirements.txt", "requirements-dev.txt")


class DeployError(Exception):
    pass


def _pip_failure_summary(output: str, limit: int = 12) -> str:
    """The most useful lines of a failed ``pip install``.

    pip writes build failures to stdout and warnings to stderr, so the tail of
    the combined output is where the actual cause lives ("No matching
    distribution found for …", "error: command 'gcc' failed", …). Noise like
    the pip-cache warning is dropped.
    """
    lines = [ln.rstrip() for ln in (output or "").splitlines() if ln.strip()]
    # Drop the harmless cache/notice noise that hides the real error.
    noise = ("WARNING: The directory", "The cache has been disabled",
             "If executing pip with sudo", "[notice]", "Requirement already satisfied")
    interesting = [ln for ln in lines if not any(n in ln for n in noise)]
    chosen = (interesting or lines)[-limit:]
    summary = "\n".join(chosen) if chosen else "see the strategy log for the full pip output"
    # A source build means the dependency has no wheel for this Python version —
    # the most common cause, and worth spelling out.
    if "Building wheel" in summary or "Building wheels for collected packages" in summary:
        summary += (
            "\n(hint: a dependency has no wheel for this Python version, so pip is "
            "building it from source — pin a version that ships a wheel, or use a "
            "Python version the dependency supports)"
        )
    return summary


class DeployService:
    def __init__(self, settings: Settings | None = None, strategy_service=None):
        self.settings = settings or get_settings()
        self.strategy_service = strategy_service or StrategyService()
        self.code_service = CodeService(self.strategy_service.store)
        self.process_service = ProcessService(self.settings, self.strategy_service)
        self.scheduler_service = SchedulerService(self.settings, self.strategy_service)

    def strategy_dir(self, strategy_id: str) -> Path:
        return self.process_service.strategy_dir(strategy_id)

    def _log(self, strategy_id: str, message: str, tag: str = "DEPLOY") -> None:
        """Deploy progress goes into the strategy's own (bounded) strategy.log —
        the single log sink the Logs tab tails/streams (golden rule 4); never a
        second sink. Multi-line output is written one line per entry."""
        for line in str(message).splitlines() or [""]:
            self.process_service._log(strategy_id, line, tag=tag)

    # --- main entry -------------------------------------------------------
    def deploy(self, strategy_id: str, wait_if_running: bool = False) -> dict:
        doc = self.strategy_service.get(strategy_id)
        # Own/verify the strategy dir BEFORE writing anything into it (the
        # deploy log below included): a pre-existing dir without the ownership
        # marker is refused — the platform never overwrites foreign content
        # (e.g. disk leftovers from a previous install under a reused STR-NNN id).
        try:
            target = ensure_managed_dir(self.strategy_dir(strategy_id))
        except UnownedDirectoryError as exc:
            # Deliberately NOT logged into the (foreign) directory.
            raise DeployError(str(exc)) from exc
        self._log(strategy_id, "deploy started")

        # (12) block replacing source files while executing.
        if self.process_service.is_running(strategy_id):
            if wait_if_running:
                self._log(strategy_id, "strategy running; waiting for safe completion")
                log.warning("strategy %s running; waiting for safe completion", strategy_id)
                import time
                for _ in range(120):
                    if not self.process_service.is_running(strategy_id):
                        break
                    time.sleep(1)
            else:
                self._log(strategy_id, "deployment blocked: strategy is running", tag="ERROR")
                raise DeployError(f"strategy {strategy_id} is running; deployment blocked")

        files = self.code_service.decode_project(doc.code)
        # The entry point is fixed to main.py (not user-configurable).
        entrypoint = DEFAULT_ENTRYPOINT

        # (3,4) validate entry point and paths.
        if entrypoint not in files:
            raise DeployError(f"entry point {entrypoint!r} is not present in the project")
        if not entrypoint.endswith(".py"):
            raise DeployError("entry point must be a Python file")
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
            self._log(strategy_id, f"staged {len(files)} file(s)")
            self._validate_python(staging, entrypoint)
            self._log(strategy_id, f"syntax check passed ({entrypoint} + all .py files)")

            # (8,9) venv + dependencies. The strategy dir was already
            # created/verified as platform-owned at the top of deploy().
            # The Python version is a platform setting, not a per-strategy field.
            self._ensure_venv(target, self.settings.default_python_version, strategy_id)
            # Dependencies + the smoke test run against the STAGED project: the live
            # dir is not synced until _sync_into (below), so on a first deploy the
            # requirements file does not yet exist under target.
            self._install_dependencies(target, staging, files, strategy_id)

            # (10,11) smoke test (import the entry point from the staged project).
            self._smoke_test(target, staging, doc, entrypoint, strategy_id)

            # (13) safely replace active managed project files.
            self._sync_into(target, staging, files)
            self._log(strategy_id, "synced staged project into the live directory")

            # (14) reconcile cron.
            self.scheduler_service.reconcile(strategy_id)
            self._log(strategy_id, "crontab reconciled")

            # (15) update deployment status.
            doc.deployment.status = "deployed"
            import datetime as _dt
            doc.deployment.last_deployed_at = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            doc.deployment.source_hash = source_hash
            self.strategy_service.store.update_document(doc)

            self._log(strategy_id, f"deployed (source hash {source_hash[:12]}…, {len(files)} files)")
            log.info("deployed %s hash=%s files=%d", strategy_id, source_hash, len(files))
            return {"strategy_id": strategy_id, "status": "deployed", "source_hash": source_hash,
                    "files": len(files), "entrypoint": entrypoint}
        except Exception as exc:  # noqa: BLE001
            # Staging guarantees the active project is untouched on failure.
            log.error("deployment failed for %s: %s", strategy_id, exc)
            self._log(strategy_id, f"DEPLOYMENT FAILED: {exc}", tag="ERROR")
            self._record_failure(doc, exc)
            raise DeployError(f"deployment failed: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # --- helpers ----------------------------------------------------------
    def _record_failure(self, doc, exc: BaseException) -> None:
        """Best-effort: mark the strategy's deployment as ``failed`` and persist it.

        Runs inside the ``deploy`` except block, just before the original error is
        re-raised as ``DeployError``. It must never raise or otherwise mask that
        original error, so it swallows its own failures.
        """
        try:
            doc.deployment.status = "failed"
            self.strategy_service.store.update_document(doc)
            log.info("recorded deployment failure for %s", doc.strategy_id)
        except Exception:  # noqa: BLE001
            log.warning("could not record deployment failure for %s", doc.strategy_id, exc_info=True)

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

    def _ensure_venv(self, target: Path, python_version: str, strategy_id: Optional[str] = None) -> Path:
        venv_python = target / ".venv" / "bin" / "python"
        wanted = ".".join((python_version or "3.12").split(".")[:2])
        if venv_python.exists():
            actual = self._venv_python_version(venv_python)
            if actual and actual != wanted:
                # A venv built with another Python must be rebuilt: reusing it
                # would silently ignore the configured version (and its
                # dependencies may have no wheel for it).
                if strategy_id:
                    self._log(strategy_id,
                              f"rebuilding the venv: it uses Python {actual}, "
                              f"the platform requires {wanted}", tag="DEPLOY")
                shutil.rmtree(target / ".venv", ignore_errors=True)
            else:
                if strategy_id:
                    self._log(strategy_id, f"reusing existing venv ({venv_python})")
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
                if strategy_id:
                    self._log(strategy_id, f"creating venv with {exe} (requested python {python_version})")
                proc = subprocess.run([exe, "-m", "venv", str(target / ".venv")], check=True,
                                      capture_output=True, text=True)
                if strategy_id and (proc.stdout or "").strip():
                    self._log(strategy_id, proc.stdout.strip())
                log.info("created venv for %s using %s", target, cand)
                return target / ".venv" / "bin" / "python"
            except subprocess.CalledProcessError:
                continue
        raise DeployError(f"could not create virtualenv for python {python_version}")

    @staticmethod
    def _venv_python_version(venv_python: Path) -> Optional[str]:
        """The major.minor version of an existing venv's interpreter ("" if unknown)."""
        try:
            proc = subprocess.run([str(venv_python), "--version"], capture_output=True,
                                  text=True, timeout=15)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not determine the version of %s: %s", venv_python, exc)
            return None
        out = (proc.stdout or proc.stderr or "").strip()
        # "Python 3.12.4" -> "3.12"
        parts = out.split()
        if len(parts) >= 2 and parts[0].lower().startswith("python"):
            return ".".join(parts[1].split(".")[:2])
        log.warning("unexpected version output from %s: %r", venv_python, out)
        return None

    def _install_dependencies(self, target: Path, staging: Path, files: dict[str, str],
                              strategy_id: Optional[str] = None) -> None:
        manifest = next((m for m in DEPENDENCY_MANIFESTS if m in files), None)
        if manifest is None:
            if strategy_id:
                self._log(strategy_id, "no requirements manifest; nothing to install")
            return
        pip = target / ".venv" / "bin" / "pip"
        if not pip.exists():
            return
        # The manifest lives in the staged project; the live dir is not updated
        # until _sync_into runs after this.
        req = staging / manifest
        if strategy_id:
            self._log(strategy_id, f"installing dependencies from {manifest} …")
        # Run pip through the venv's interpreter and give it a writable cache
        # directory: the service account's HOME points at the platform root
        # (root-owned), which makes pip disable its cache with a warning.
        env = dict(os.environ)
        env["PIP_CACHE_DIR"] = str(target / ".cache" / "pip")
        env.pop("PIP_USER", None)
        env["PYTHONNOUSERSITE"] = "1"
        try:
            proc = subprocess.run(
                [str(target / ".venv" / "bin" / "python"), "-m", "pip", "install", "-r", str(req)],
                check=True, capture_output=True, text=True, env=env,
                timeout=self.settings.dependency_install_timeout_seconds,
            )
            # Full pip output into the strategy log — it is the only place the
            # user can see what was installed (and why a version was skipped).
            for stream_name, out in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                if out and out.strip():
                    if strategy_id:
                        self._log(strategy_id, f"pip {stream_name}:\n{out.rstrip()}", tag="DEPLOY")
            log.info("installed dependencies from %s for %s", manifest, target)
            if strategy_id:
                self._log(strategy_id, "dependencies installed")
        except subprocess.CalledProcessError as exc:
            # pip prints build/resolution failures to STDOUT (the stderr often
            # only carries warnings) — the message must show the real cause.
            combined = ((exc.stdout or "") + "\n" + (exc.stderr or "")).strip()
            if strategy_id:
                self._log(strategy_id, f"pip failed (exit {exc.returncode}):\n{combined}",
                          tag="ERROR")
            raise DeployError(
                "dependency installation failed: " + _pip_failure_summary(combined)
            ) from exc
        except subprocess.TimeoutExpired as exc:
            output = ((exc.stdout or "") + "\n" + (exc.stderr or "")).strip()
            if isinstance(exc.stdout, bytes):
                output = exc.stdout.decode("utf-8", "replace")
            if strategy_id:
                self._log(strategy_id, f"pip timed out after "
                                       f"{self.settings.dependency_install_timeout_seconds}s:\n{output}",
                          tag="ERROR")
            raise DeployError(
                f"dependency installation timed out after "
                f"{self.settings.dependency_install_timeout_seconds}s — usually a dependency "
                "without a wheel for this Python version (a source build). Pin a version "
                "that ships a wheel, or use a Python version the dependency supports."
            ) from exc

    def _smoke_test(self, target: Path, staging: Path, doc, entrypoint: str,
                    strategy_id: Optional[str] = None) -> None:
        """Configurable import smoke test. Importing the module must not raise
        at import time. The entry point should define ``run(config)`` or be a
        standalone script; we only import to catch import-time errors."""
        venv_python = target / ".venv" / "bin" / "python"
        if not venv_python.exists():
            return
        module = entrypoint[:-3] if entrypoint.endswith(".py") else entrypoint
        # Only import top-level modules (not dotted paths) for the smoke test.
        top = module.split("/")[0]
        if strategy_id:
            self._log(strategy_id, f"smoke test: importing {top} with the strategy venv")
        try:
            result = subprocess.run(
                [str(venv_python), "-c", f"import importlib; importlib.import_module('{top}')"],
                cwd=str(staging), capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            if strategy_id:
                self._log(strategy_id, "smoke test timed out after 30s", tag="ERROR")
            log.warning("smoke test timed out for %s", top)
            raise DeployError(f"smoke test timed out importing {top}") from exc
        except Exception as exc:  # noqa: BLE001  # cannot even run the smoke command
            log.warning("smoke test could not run for %s: %s", top, exc)
            return
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            if strategy_id:
                self._log(strategy_id,
                          f"smoke import returned {result.returncode}"
                          + (f":\n{detail}" if detail else ""), tag="ERROR")
            log.warning("smoke import returned %d for %s", result.returncode, top)
            # A strategy that cannot import will crash on every cron tick —
            # that is a broken deployment, not a deployed one.
            last = detail.splitlines()[-1] if detail else "no error output"
            raise DeployError(f"smoke test failed: importing {top} raised an error ({last})")
        if strategy_id:
            self._log(strategy_id, "smoke test passed")

    def _sync_into(self, target: Path, staging: Path, files: dict[str, str]) -> None:
        """Mirror the project into ``target``.

        Removes previously-deployed top-level project entries that no longer
        exist in the current project, then writes all current files. Platform
        managed resources (``.venv/``, ``strategy.log``) are always preserved.
        """
        reserved = RESERVED_TOP_LEVEL | {".venv"}
        # Ownership guard: never mirror into a directory the platform does not
        # manage (it may hold foreign data a blind rmtree/write would damage).
        if target.exists() and not is_managed(target):
            raise DeployError(
                f"refusing to sync into '{target}': directory exists without "
                f"the '{MANAGED_MARKER}' ownership marker"
            )
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
