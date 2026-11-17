"""Process management service.

Tracks process IDs and runtime state locally under the platform runtime
directory (spec sections 12, 17). Uses OS-level file locking (never S3) to
prevent duplicate execution. Launches a strategy's entry point with the
strategy's own virtual-environment interpreter, captures stdout/stderr into the
single strategy log file and enforces an execution timeout.

NOTE: This runs on the target EC2 host. On local/dev machines the paths point
at the configured ``platform_root`` and a strategy may not be deployed, in which
case operations no-op safely.
"""
from __future__ import annotations

import json
import os
import signal
import shlex
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.paths import (
    UnownedDirectoryError,
    ensure_managed_dir,
    ensure_managed_strategies_root,
    is_managed,
)
from app.models.strategy import DEFAULT_ENTRYPOINT
from app.models.system import ProcessInfo, RUNTIME_STATES, RuntimeStatus
from app.services.strategy_service import StrategyService

log = get_logger("services.process")


class ProcessServiceError(Exception):
    pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def file_lock(lock_path: Path, blocking: bool = True):
    """OS-level exclusive advisory lock for duplicate-execution protection."""
    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            raise ProcessServiceError("could not acquire execution lock (already running?)")
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class ProcessService:
    def __init__(self, settings: Settings | None = None, strategy_service=None,
                 broker_service=None, tick_service=None):
        self.settings = settings or get_settings()
        self.strategy_service = strategy_service or StrategyService()
        # Optional broker-token provider: when the strategy's owner has a
        # connected broker, its (daily-refreshed) access token is injected into
        # the runtime config. None → no injection (tests, unit use).
        self.broker_service = broker_service
        # Optional tick-gateway provider: the owner's enabled market-data
        # feeds are injected as the runtime config's ``data`` section.
        self.tick_service = tick_service
        self.state_dir = settings.state_dir_path()
        self.locks_dir = settings.locks_dir_path()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        # Ownership guard (fail fast): the strategies root must be platform
        # created/adoptable. A root holding foreign content without the
        # ownership marker refuses service — a misconfigured STRATEGIES_DIR can
        # never silently overwrite existing strategies. Individual strategy
        # dirs are still guarded per-directory at deploy and delete time.
        ensure_managed_strategies_root(self.settings.strategies_dir_path())

    # --- paths ------------------------------------------------------------
    def strategy_dir(self, strategy_id: str) -> Path:
        return self.settings.strategies_dir_path() / strategy_id

    def venv_python(self, strategy_id: str) -> Path:
        return self.strategy_dir(strategy_id) / ".venv" / "bin" / "python"

    def log_path(self, strategy_id: str) -> Path:
        return self.strategy_dir(strategy_id) / "strategy.log"

    def state_path(self, strategy_id: str) -> Path:
        return self.state_dir / f"{strategy_id}.json"

    def lock_path(self, strategy_id: str) -> Path:
        return self.locks_dir / f"{strategy_id}.lock"

    def runtime_config_path(self, strategy_id: str) -> Path:
        # Runtime config is NOT part of the user's saved Monaco project.
        return self.strategy_dir(strategy_id) / ".venv" / "share" / "runtime_config.json"

    # --- state helpers ----------------------------------------------------
    def read_state(self, strategy_id: str) -> dict:
        p = self.state_path(strategy_id)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _write_state(self, strategy_id: str, state: dict) -> None:
        p = self.state_path(strategy_id)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # --- public API -------------------------------------------------------
    def is_running(self, strategy_id: str) -> bool:
        st = self.read_state(strategy_id)
        pid = st.get("pid")
        return bool(pid) and _pid_alive(pid)

    def has_live_positions_guard(self, strategy_id: str) -> bool:
        """Heuristic guard: a strategy is considered to manage live positions
        if a kill-switch flag has been set locally. This never auto-cancels
        broker orders; it only blocks destructive platform actions."""
        st = self.read_state(strategy_id)
        return bool(st.get("live_positions_guard", False))

    def runtime_status(self, strategy_id: str) -> RuntimeStatus:
        try:
            doc = self.strategy_service.get(strategy_id)
        except Exception:  # noqa: BLE001
            doc = None
        st = self.read_state(strategy_id)
        state = st.get("state", "stopped")
        proc = None
        if st.get("pid") and _pid_alive(st["pid"]):
            state = "running"
            proc = ProcessInfo(
                strategy_id=strategy_id,
                schedule_id=st.get("schedule_id"),
                state="running",
                pid=st["pid"],
                started_at=st.get("started_at"),
            )
        elif state == "running":
            # The state file says running but the pid is gone: the run exited
            # on its own (crashed or finished). The runner is ephemeral — it
            # exits right after launching, so nothing marks the exit — and its
            # timeout thread dies with it. The first reader therefore HEALS the
            # stale record: the reported state is stopped, and the file is
            # corrected so every later reader (and the UI chip) agrees.
            self._heal_stale_run(strategy_id, st)
            state = "stopped"
        deploy_status = doc.deployment.status if doc else "not_deployed"
        if not doc or deploy_status == "not_deployed":
            state = "not_deployed"
        return RuntimeStatus(
            strategy_id=strategy_id,
            state=state,
            process=proc,
            has_live_positions_guard=self.has_live_positions_guard(strategy_id),
            deployment_status=deploy_status,
            deployed_source_hash=doc.deployment.source_hash if doc else None,
            updated_at=st.get("updated_at"),
        )

    def _heal_stale_run(self, strategy_id: str, st: dict) -> None:
        """Correct a state file that still says ``running`` for a dead pid.

        Best-effort: a failed heal only means the next reader tries again."""
        try:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            st.update({"state": "stopped", "ended_at": now,
                       "healed_at": now,
                       "healed_reason": "process no longer alive"})
            self._write_state(strategy_id, st)
            log.info("healed stale run state for %s (pid %s gone)",
                     strategy_id, st.get("pid"))
        except Exception:  # noqa: BLE001 — never fail a status read here
            log.warning("could not heal stale run state for %s", strategy_id,
                        exc_info=True)

    def start(self, strategy_id: str, schedule_id: Optional[str] = None,
              command: Optional[str] = None, allow_concurrent: Optional[bool] = None) -> ProcessInfo:
        doc = self.strategy_service.get(strategy_id)
        if doc.deployment.status != "deployed":
            raise ProcessServiceError("strategy is not deployed; deploy first")
        # Concurrency is a platform setting, not a per-strategy field.
        concurrent = self.settings.allow_concurrent_runs if allow_concurrent is None else allow_concurrent
        if not concurrent and self.is_running(strategy_id):
            self._log(strategy_id, "SKIP: strategy already running (concurrent runs disabled)")
            raise ProcessServiceError("duplicate execution prevented: strategy already running")
        with file_lock(self.lock_path(strategy_id), blocking=concurrent):
            if not concurrent and self.is_running(strategy_id):
                self._log(strategy_id, "SKIP: another run acquired the lock")
                raise ProcessServiceError("duplicate execution prevented: lock held")
            return self._launch(doc, schedule_id, command)

    def _launch(self, doc, schedule_id: Optional[str], command: Optional[str] = None) -> ProcessInfo:
        sid = doc.strategy_id
        python = self.venv_python(sid)
        if not python.exists():
            raise ProcessServiceError(f"venv interpreter missing for {sid} (did you deploy?)")
        # Write runtime config (separate from the user's Monaco project).
        self._write_runtime_config(doc)
        log_file = self.log_path(sid)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # The entry point is fixed to main.py; the schedule's ``command`` supplies
        # the arguments. Split it into an argument array and append it to the
        # invocation — no shell interpolation (golden rule 6).
        entrypoint = DEFAULT_ENTRYPOINT
        args = shlex.split(command) if command else []
        timeout_seconds = self.settings.default_run_timeout_seconds
        cmd = [str(python), str(self.strategy_dir(sid) / entrypoint), *args]
        log_handle = open(log_file, "a", encoding="utf-8")
        log_handle.write(f"\n=== run {schedule_id or 'manual'} @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.strategy_dir(sid)),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=self._run_env(doc),
        )
        st = {
            "pid": proc.pid,
            "schedule_id": schedule_id,
            "state": "running",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "entrypoint": entrypoint,
            "args": args,
            "timeout_seconds": timeout_seconds,
        }
        self._write_state(sid, st)
        self._log(sid, f"started pid={proc.pid} entrypoint={entrypoint} args={args}")
        # Enforce timeout in the background.
        import threading
        threading.Thread(target=self._enforce_timeout, args=(sid, proc.pid, timeout_seconds),
                         daemon=True).start()
        return ProcessInfo(strategy_id=sid, schedule_id=schedule_id, state="running",
                           pid=proc.pid, started_at=st["started_at"])

    def _enforce_timeout(self, sid: str, pid: int, timeout_seconds: int) -> None:
        # KNOWN LIMITATION: this runs as a daemon thread in the *caller's*
        # process. The runner (the only launcher) exits right after start(),
        # so the thread — and with it the timeout enforcement — dies with the
        # runner: DEFAULT_RUN_TIMEOUT_SECONDS is currently NOT enforced for
        # runner-launched runs. Enforcement that works needs a process that
        # outlives the launch (a watcher step in the runner, or a platform
        # watchdog); do not assume this loop fires.
        deadline = time.time() + timeout_seconds
        while _pid_alive(pid) and time.time() < deadline:
            time.sleep(1)
        if _pid_alive(pid):
            self._log(sid, f"TIMEOUT after {timeout_seconds}s — terminating pid={pid}")
            self._terminate(pid, graceful=False, log_tag=f"timeout pid={pid}")
            self._mark_finished(sid, exit_code=124, state="failed")

    def stop(self, strategy_id: str, graceful: bool = True) -> dict:
        st = self.read_state(strategy_id)
        pid = st.get("pid")
        if not pid or not _pid_alive(pid):
            # A stale record (the run already exited on its own) must not keep
            # saying running — heal it, then report the truth (the API maps
            # this to 409).
            if st.get("state") == "running":
                self._heal_stale_run(strategy_id, st)
            return {"stopped": False, "reason": "not running"}
        result = self._terminate(pid, graceful=graceful)
        self._mark_finished(strategy_id, exit_code=st.get("exit_code", 0), state="stopped")
        return {"stopped": True, **result}

    def restart(self, strategy_id: str, schedule_id: Optional[str] = None) -> ProcessInfo:
        self.stop(strategy_id, graceful=True)
        time.sleep(0.5)
        return self.start(strategy_id, schedule_id=schedule_id)

    def _terminate(self, pid: int, graceful: bool, log_tag: str = "") -> dict:
        grace = self.settings.process_grace_period_seconds
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            return {"signal": "none", "reason": "process already gone"}
        deadline = time.time() + grace
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.25)
        if _pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                pass
            return {"signal": "SIGKILL", "forced": True, "grace_period": grace}
        return {"signal": "SIGTERM", "grace_period": grace}

    def _mark_finished(self, sid: str, exit_code: int, state: str) -> None:
        st = self.read_state(sid)
        st.update({"state": state, "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "exit_code": exit_code, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        self._write_state(sid, st)

    def set_kill_switch(self, strategy_id: str, active: bool = True) -> None:
        """Prevent new order submissions without disabling position mgmt logic."""
        st = self.read_state(strategy_id)
        st["kill_switch"] = active
        st["live_positions_guard"] = active
        self._write_state(strategy_id, st)

    def remove_local_resources(self, strategy_id: str) -> None:
        import shutil
        d = self.strategy_dir(strategy_id)
        if d.exists():
            if not is_managed(d):
                raise ProcessServiceError(
                    f"refusing to delete '{d}': directory exists without the "
                    f"platform ownership marker"
                )
            shutil.rmtree(d, ignore_errors=True)
        sp = self.state_path(strategy_id)
        if sp.exists():
            sp.unlink(missing_ok=True)
        lp = self.lock_path(strategy_id)
        if lp.exists():
            lp.unlink(missing_ok=True)

    # --- helpers ----------------------------------------------------------
    def _log(self, sid: str, message: str, tag: str = "PROCESS") -> None:
        try:
            self.log_path(sid).parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path(sid), "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | INFO | {tag} | {message}\n")
        except Exception:  # noqa: BLE001
            pass

    def _write_runtime_config(self, doc) -> None:
        p = self.runtime_config_path(doc.strategy_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Runtime values now come from platform settings (not the strategy doc),
        # so the generated runtime config keeps the same shape for strategies that
        # read it.
        payload = {
            "strategy_id": doc.strategy_id,
            "configuration": doc.configuration.model_dump(),
            "runtime": {
                "python_version": self.settings.default_python_version,
                "timeout_seconds": self.settings.default_run_timeout_seconds,
                "allow_concurrent_runs": self.settings.allow_concurrent_runs,
            },
            "schedules": [s.model_dump() for s in doc.schedules],
            "kill_switch": self.read_state(doc.strategy_id).get("kill_switch", False),
            # Connected brokers of the strategy's owner: the daily-refreshed
            # access token, so the strategy reads
            # config["brokers"]["<broker>"]["access_token"]. Written fresh on
            # EVERY launch, so a run always picks up the latest token.
            "brokers": self._broker_section(doc),
            # The owner's enabled tick feeds: where the strategy's market-data
            # gateway lives (a localhost Redis channel), so it reads
            # config["data"]["gateway"]["<broker>"]["channel"] — never a
            # hard-coded channel or a socket of its own.
            "data": self._data_section(doc),
        }
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _broker_section(self, doc) -> dict:
        """The owner's broker credentials, or ``{}`` when none / unavailable."""
        if self.broker_service is None or not doc.owner_id:
            return {}
        try:
            section = self.broker_service.runtime_credentials(doc.owner_id)
        except Exception as exc:  # noqa: BLE001 — a broker must not stop a run
            self._log(doc.strategy_id,
                      f"BROKER: credentials unavailable ({type(exc).__name__})",
                      tag="ERROR")
            return {}
        if section:
            injected = ", ".join(
                f"{broker} (generated {meta.get('generated_at')})"
                for broker, meta in sorted(section.items())
            )
            self._log(doc.strategy_id,
                      f"BROKER: access tokens injected: {injected}")
        return section

    def _data_section(self, doc) -> dict:
        """The owner's enabled tick feeds (the ``data.gateway`` contract), or
        ``{}`` when none / unavailable — like ``_broker_section``, a broken
        provider must stop the *injection*, never the run."""
        if self.tick_service is None or not doc.owner_id:
            return {}
        try:
            section = self.tick_service.data_section(doc.owner_id)
        except Exception as exc:  # noqa: BLE001 — data must not stop a run
            self._log(doc.strategy_id,
                      f"DATA: gateway info unavailable ({type(exc).__name__})",
                      tag="ERROR")
            return {}
        gateway = section.get("gateway") or {}
        if gateway:
            injected = ", ".join(
                f"{broker} -> {meta['channel']} "
                f"({len(meta.get('subscriptions', []))} symbols)"
                for broker, meta in sorted(gateway.items()))
            self._log(doc.strategy_id, f"DATA: tick gateway injected: {injected}")
        return section

    def _run_env(self, doc) -> dict:
        env = dict(os.environ)
        env["QUANT_STRATEGY_ID"] = doc.strategy_id
        env["QUANT_RUNTIME_CONFIG"] = str(self.runtime_config_path(doc.strategy_id))
        return env
