"""Broker connections: enable, connect-retry, disable and token refresh.

Offline: credentials go to a fake Secrets Manager, the Dhan HTTP call is
stubbed, and the scheduler is a spy — no real AWS, no network (§7).
"""
import pytest

from app.models.strategy import StrategyCreate
from app.models.user import SignupRequest
from app.models.broker import DhanEnableRequest
from app.services.broker_scheduler import job_id
from app.services.broker_service import BrokerError, BrokerService


def _user_service_for(settings, fake_s3):
    from app.services.user_service import UserService
    from app.storage.user_store import UserStore

    return UserService(settings, UserStore(fake_s3))


class FakeSecretStore:
    """In-memory stand-in for Secrets Manager."""

    def __init__(self):
        self.secrets: dict = {}
        self.deleted: list = []

    def put_secret(self, key, payload):
        self.secrets[key] = dict(payload)

    def get_secret(self, key):
        value = self.secrets.get(key)
        return dict(value) if value is not None else None

    def delete_secret(self, key):
        self.deleted.append(key)
        self.secrets.pop(key, None)


class SpyScheduler:
    """Records scheduling calls without running anything."""

    def __init__(self):
        self.scheduled: list = []
        self.unscheduled: list = []

    def schedule_daily(self, user_id, hour=None):
        self.scheduled.append(user_id)

    def unschedule(self, user_id):
        self.unscheduled.append(user_id)

    def next_run(self, user_id):
        return "2026-11-16T08:00:00+05:30" if user_id in self.scheduled else None


@pytest.fixture
def broker_service(store, settings, fake_s3):
    from app.services.broker_service import BrokerService
    from app.storage.broker_store import BrokerStore

    secrets = FakeSecretStore()
    scheduler = SpyScheduler()
    svc = BrokerService(settings, BrokerStore(fake_s3), secrets, scheduler)
    svc.secrets = secrets
    svc.scheduler = scheduler
    yield svc
    svc._inflight.clear()


@pytest.fixture
def no_threads(monkeypatch):
    """Run connection attempts synchronously so tests are deterministic."""
    monkeypatch.setattr("app.services.broker_service.BrokerService._spawn_attempt",
                        lambda self, user_id: self._attempt(user_id))


@pytest.fixture
def dhan_ok(monkeypatch):
    """Dhan answers with a token on the first attempt."""
    monkeypatch.setattr("app.services.broker_service._dhan_request",
                        lambda url, timeout=10: (200, {"accessToken": "tok-123",
                                                       "status": "success"}))


@pytest.fixture
def dhan_always_invalid_totp(monkeypatch):
    """Dhan keeps answering Invalid TOTP (and never sleeps for real)."""
    monkeypatch.setattr("app.services.broker_service._dhan_request",
                        lambda url, timeout=10: (200, {"status": "error",
                                                       "message": "Invalid TOTP"}))
    monkeypatch.setattr("app.services.broker_service._sleep", lambda s: None)


VALID = dict(totp_secret="JBSWY3DPEHPK3PXP", client_id="12345678", pin="112233")


# --- enable -----------------------------------------------------------------
def test_enable_rejects_an_invalid_totp_secret(broker_service):
    with pytest.raises(BrokerError):
        broker_service.enable("USR-1", DhanEnableRequest(
            totp_secret="not-base32!!", client_id="12345678", pin="112233"))
    # Nothing was stored — a typo must not leave credentials behind.
    assert broker_service.secrets.secrets == {}
    assert broker_service.store.list_for_user("USR-1") == []


def test_enable_stores_secret_and_connects(broker_service, no_threads, dhan_ok):
    broker_service.enable("USR-1", DhanEnableRequest(**VALID))
    record = broker_service.store.get("USR-1", "dhan")
    assert record.status == "connected"
    # Credentials live in Secrets Manager only…
    assert "USR-1/dhan" in broker_service.secrets.secrets
    stored = broker_service.secrets.secrets["USR-1/dhan"]
    assert stored["pin"] == "112233" and "totp_secret" in stored
    # …the S3 record carries metadata only.
    record = broker_service.store.get("USR-1", "dhan")
    assert record.status == "connected"
    assert record.last_token_at is not None
    assert "pin" not in record.model_dump().values()
    # The access token was stored separately from the credentials.
    assert broker_service.secrets.secrets["USR-1/dhan-token"]["access_token"] == "tok-123"
    # …and the daily refresh was armed.
    assert broker_service.scheduler.scheduled == ["USR-1"]


def test_enable_retries_until_totp_cycle(broker_service, no_threads, monkeypatch):
    """First attempt invalid, second succeeds — still ends up connected."""
    answers = [(200, {"status": "error", "message": "Invalid TOTP"}),
               (200, {"accessToken": "tok-2", "status": "success"})]
    monkeypatch.setattr("app.services.broker_service._dhan_request",
                        lambda url, timeout=10: answers.pop(0))
    waits: list = []
    monkeypatch.setattr("app.services.broker_service._sleep", waits.append)
    broker_service.enable("USR-2", DhanEnableRequest(**VALID))
    assert broker_service.store.get("USR-2", "dhan").status == "connected"
    assert len(waits) == 1 and 0 < waits[0] <= 31  # waited for the next cycle


def test_enable_fails_when_dhan_keeps_rejecting(broker_service, no_threads,
                                                dhan_always_invalid_totp):
    broker_service.enable("USR-3", DhanEnableRequest(**VALID))
    record = broker_service.store.get("USR-3", "dhan")
    assert record.status == "failed"
    assert "3 attempts" in record.last_error
    # No daily job for a failed connection.
    assert broker_service.scheduler.scheduled == []


def test_enable_surfaces_a_dhan_refusal(broker_service, no_threads, monkeypatch):
    monkeypatch.setattr("app.services.broker_service._dhan_request",
                        lambda url, timeout=10: (200, {"status": "error",
                                                       "message": "Invalid PIN"}))
    broker_service.enable("USR-4", DhanEnableRequest(**VALID))
    record = broker_service.store.get("USR-4", "dhan")
    assert record.status == "failed"
    assert "Invalid PIN" in record.last_error


def test_enable_does_not_leak_secrets_into_logs(broker_service, no_threads,
                                                dhan_ok, caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="services.broker"):
        broker_service.enable("USR-5", DhanEnableRequest(**VALID))
    text = caplog.text
    assert "112233" not in text and "JBSWY3DPEHPK3PXP" not in text
    assert "tok-123" not in text


# --- retry / disable --------------------------------------------------------
def test_retry_reuses_the_stored_credentials(broker_service, no_threads,
                                             monkeypatch):
    broker_service.enable("USR-6", DhanEnableRequest(**VALID))
    # Force a failure, then retry.
    broker_service.store.get("USR-6", "dhan").status = "failed"
    monkeypatch.setattr("app.services.broker_service._dhan_request",
                        lambda url, timeout=10: (200, {"accessToken": "tok-9"}))
    doc = broker_service.retry("USR-6")
    assert broker_service.store.get("USR-6", "dhan").status == "connected"
    assert broker_service.secrets.secrets["USR-6/dhan-token"]["access_token"] == "tok-9"


def test_disable_removes_secret_job_and_record(broker_service, no_threads, dhan_ok):
    broker_service.enable("USR-7", DhanEnableRequest(**VALID))
    assert broker_service.scheduler.scheduled == ["USR-7"]
    broker_service.disable("USR-7")
    assert "USR-7/dhan" in broker_service.secrets.deleted
    assert "USR-7/dhan-token" in broker_service.secrets.deleted
    assert broker_service.scheduler.unscheduled == ["USR-7"]
    assert broker_service.store.get("USR-7", "dhan") is None


# --- the scheduled refresh --------------------------------------------------
def test_refresh_token_updates_the_record(broker_service, no_threads, dhan_ok,
                                          monkeypatch):
    broker_service.enable("USR-8", DhanEnableRequest(**VALID))
    monkeypatch.setattr("app.services.broker_service._dhan_request",
                        lambda url, timeout=10: (200, {"accessToken": "tok-new"}))
    broker_service.refresh_token("USR-8")
    record = broker_service.store.get("USR-8", "dhan")
    assert record.status == "connected"
    assert broker_service.secrets.secrets["USR-8/dhan-token"]["access_token"] == "tok-new"
    # A refresh failure keeps the connection (credentials remain valid)…
    monkeypatch.setattr("app.services.broker_service._dhan_request",
                        lambda url, timeout=10: (200, {"status": "error",
                                                       "message": "Invalid TOTP"}))
    monkeypatch.setattr("app.services.broker_service._sleep", lambda s: None)
    broker_service.refresh_token("USR-8")
    record = broker_service.store.get("USR-8", "dhan")
    assert record.status == "connected"
    assert "invalid TOTP" in record.last_error
    # …and tomorrow's run stays armed.
    assert "USR-8" in broker_service.scheduler.scheduled


def test_startup_reconcile_repairs_interrupted_attempts(broker_service, no_threads,
                                                        dhan_ok):
    broker_service.enable("USR-9", DhanEnableRequest(**VALID))
    # Simulate a restart mid-attempt: the record stayed "connecting".
    interrupted = broker_service.store.get("USR-9", "dhan")
    interrupted.status = "connecting"
    broker_service.store.put(interrupted)
    broker_service.reconcile_on_startup()
    record = broker_service.store.get("USR-9", "dhan")
    assert record.status == "failed"
    assert "interrupted" in record.last_error


def test_list_is_scoped_to_the_calling_user(broker_service, no_threads, dhan_ok):
    broker_service.enable("USR-A", DhanEnableRequest(**VALID))
    broker_service.enable("USR-B", DhanEnableRequest(**VALID))
    assert [b.broker for b in broker_service.list_for_user("USR-A").brokers] == ["dhan"]
    assert broker_service.list_for_user("USR-ZZ").brokers == []


def test_scheduler_job_id_is_stable():
    assert job_id("USR-1") == "dhan-token-USR-1"


def test_runner_wires_the_broker_service_into_process_service():
    """Regression: the runner (the cron/manual path) once constructed
    ``ProcessService`` *without* the broker service, so every run got an empty
    ``brokers`` config while the API process was wired correctly — the token
    silently vanished from cron-fired runs. A source-level check keeps the
    runner's construction honest."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "scripts" / "runner.py"
    text = src.read_text()
    m = re.search(r"proc\s*=\s*ProcessService\(([^)]*)\)", text)
    assert m, "runner must construct a ProcessService"
    args = [a.strip() for a in m.group(1).replace("\n", " ").split(",")]
    assert "broker_service" in args, (
        "runner's ProcessService must receive the broker service "
        "(the runtime-config token injection lives there)"
    )


# --- the strategy contract: tokens in the runtime config ---------------------
def test_runtime_credentials_only_for_connected_users(broker_service, no_threads,
                                                      dhan_ok):
    from app.models.broker import DhanEnableRequest

    broker_service.enable("USR-C1", DhanEnableRequest(**VALID))
    section = broker_service.runtime_credentials("USR-C1")
    assert section["dhan"]["access_token"] == "tok-123"
    assert section["dhan"]["client_id"] == "12345678"
    assert section["dhan"]["generated_at"]
    # No credential fields beyond the token + client id.
    assert set(section["dhan"]) == {"client_id", "access_token", "generated_at"}

    # Disconnected / unknown users get an empty section.
    broker_service.disable("USR-C1")
    assert broker_service.runtime_credentials("USR-C1") == {}
    assert broker_service.runtime_credentials("USR-NOPE") == {}
    assert broker_service.runtime_credentials(None) == {}


def test_runtime_credentials_survive_a_secrets_failure(broker_service,
                                                       no_threads, dhan_ok):
    from app.models.broker import DhanEnableRequest
    from app.storage.secret_store import SecretStoreError

    broker_service.enable("USR-C2", DhanEnableRequest(**VALID))

    def broken(key):
        raise SecretStoreError("secretsmanager down")

    broker_service.secrets.get_secret = broken
    # A broken Secrets Manager must NOT raise — it just yields no credentials,
    # so the strategy run continues (without a token) instead of crashing.
    assert broker_service.runtime_credentials("USR-C2") == {}


def _deployed_owned(strategy_service, settings, owner_id) -> str:
    doc = strategy_service.create(StrategyCreate(name="Brokered",
                                                 files={"main.py": "print(1)"}),
                                  owner_id=owner_id)
    doc = strategy_service.get(doc.strategy_id)
    doc.deployment.status = "deployed"
    strategy_service.store.update_document(doc)
    venv_bin = settings.strategies_dir_path() / doc.strategy_id / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "python").write_text("#!/bin/sh\n")
    return doc.strategy_id


def test_runtime_config_carries_the_broker_token(strategy_service, settings,
                                                 fake_s3, monkeypatch, no_threads,
                                                 dhan_ok):
    """The runner-injected runtime config contains the owner's Dhan token."""
    import json

    from app.models.broker import DhanEnableRequest
    from app.services.broker_service import BrokerService
    from app.services.process_service import ProcessService
    from app.storage.broker_store import BrokerStore

    secrets = FakeSecretStore()
    broker = BrokerService(settings, BrokerStore(fake_s3), secrets, SpyScheduler())
    user_id = "USR-OWNER"
    user_service = _user_service_for(settings, fake_s3)
    user_service.signup(SignupRequest(full_name="Owner", mobile="9000011111",
                                      password="pw12345"))
    user_id = user_service.get_by_mobile("9000011111").user_id
    broker.enable(user_id, DhanEnableRequest(**VALID))

    sid = _deployed_owned(strategy_service, settings, user_id)
    proc = ProcessService(settings, strategy_service, broker)
    # (No real launch — write the config exactly as the launcher does.)
    doc = strategy_service.get(sid)
    proc._write_runtime_config(doc)
    payload = json.loads((settings.strategies_dir_path() / sid / ".venv" / "share"
                          / "runtime_config.json").read_text())
    assert payload["brokers"]["dhan"]["access_token"] == "tok-123"
    assert payload["brokers"]["dhan"]["client_id"] == "12345678"


def test_runtime_config_without_a_broker_has_no_brokers_key(strategy_service,
                                                            settings):
    import json

    from app.services.process_service import ProcessService

    sid = _deployed_owned(strategy_service, settings, None)
    proc = ProcessService(settings, strategy_service)  # no broker service at all
    doc = strategy_service.get(sid)
    proc._write_runtime_config(doc)
    payload = json.loads((settings.strategies_dir_path() / sid / ".venv" / "share"
                          / "runtime_config.json").read_text())
    assert payload["brokers"] == {}
