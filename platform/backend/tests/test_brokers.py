"""Broker connections: enable, connect-retry, disable and token refresh.

Offline: credentials go to a fake parameter store, the Dhan HTTP call is
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

    def schedule_daily(self, user_id, broker="dhan", hour=None):
        self.scheduled.append((user_id, broker))

    def unschedule(self, user_id, broker="dhan"):
        self.unscheduled.append((user_id, broker))

    def next_run(self, user_id, broker="dhan"):
        return "2026-11-16T08:00:00+05:30" if (user_id, broker) in self.scheduled else None


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
                        lambda self, user_id, broker="dhan":
                        self._attempt(user_id, broker))


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


VALID = dict(totp_secret="JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP", client_id="12345678", pin="112233")


# --- enable -----------------------------------------------------------------
def test_enable_rejects_an_invalid_totp_secret(broker_service):
    with pytest.raises(BrokerError):
        broker_service.enable("USR-1", "dhan", DhanEnableRequest(
            totp_secret="not-base32!!", client_id="12345678", pin="112233"))
    # Nothing was stored — a typo must not leave credentials behind.
    assert broker_service.secrets.secrets == {}
    assert broker_service.store.list_for_user("USR-1") == []


def test_enable_stores_secret_and_connects(broker_service, no_threads, dhan_ok):
    broker_service.enable("USR-1", "dhan", DhanEnableRequest(**VALID))
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
    assert broker_service.scheduler.scheduled == [("USR-1", "dhan")]


def test_enable_retries_until_totp_cycle(broker_service, no_threads, monkeypatch):
    """First attempt invalid, second succeeds — still ends up connected."""
    answers = [(200, {"status": "error", "message": "Invalid TOTP"}),
               (200, {"accessToken": "tok-2", "status": "success"})]
    monkeypatch.setattr("app.services.broker_service._dhan_request",
                        lambda url, timeout=10: answers.pop(0))
    waits: list = []
    monkeypatch.setattr("app.services.broker_service._sleep", waits.append)
    broker_service.enable("USR-2", "dhan", DhanEnableRequest(**VALID))
    assert broker_service.store.get("USR-2", "dhan").status == "connected"
    assert len(waits) == 1 and 0 < waits[0] <= 31  # waited for the next cycle


def test_enable_fails_when_dhan_keeps_rejecting(broker_service, no_threads,
                                                dhan_always_invalid_totp):
    broker_service.enable("USR-3", "dhan", DhanEnableRequest(**VALID))
    record = broker_service.store.get("USR-3", "dhan")
    assert record.status == "failed"
    assert "3 attempts" in record.last_error
    # No daily job for a failed connection.
    assert broker_service.scheduler.scheduled == []


def test_enable_surfaces_a_dhan_refusal(broker_service, no_threads, monkeypatch):
    monkeypatch.setattr("app.services.broker_service._dhan_request",
                        lambda url, timeout=10: (200, {"status": "error",
                                                       "message": "Invalid PIN"}))
    broker_service.enable("USR-4", "dhan", DhanEnableRequest(**VALID))
    record = broker_service.store.get("USR-4", "dhan")
    assert record.status == "failed"
    assert "Invalid PIN" in record.last_error


def test_enable_does_not_leak_secrets_into_logs(broker_service, no_threads,
                                                dhan_ok, caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="services.broker"):
        broker_service.enable("USR-5", "dhan", DhanEnableRequest(**VALID))
    text = caplog.text
    assert "112233" not in text and "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP" not in text
    assert "tok-123" not in text


# --- retry / disable --------------------------------------------------------
def test_retry_reuses_the_stored_credentials(broker_service, no_threads,
                                             monkeypatch):
    broker_service.enable("USR-6", "dhan", DhanEnableRequest(**VALID))
    # Force a failure, then retry.
    broker_service.store.get("USR-6", "dhan").status = "failed"
    monkeypatch.setattr("app.services.broker_service._dhan_request",
                        lambda url, timeout=10: (200, {"accessToken": "tok-9"}))
    doc = broker_service.retry("USR-6")
    assert broker_service.store.get("USR-6", "dhan").status == "connected"
    assert broker_service.secrets.secrets["USR-6/dhan-token"]["access_token"] == "tok-9"


def test_disable_removes_secret_job_and_record(broker_service, no_threads, dhan_ok):
    broker_service.enable("USR-7", "dhan", DhanEnableRequest(**VALID))
    assert broker_service.scheduler.scheduled == [("USR-7", "dhan")]
    broker_service.disable("USR-7")
    assert "USR-7/dhan" in broker_service.secrets.deleted
    assert "USR-7/dhan-token" in broker_service.secrets.deleted
    assert broker_service.scheduler.unscheduled == [("USR-7", "dhan")]
    assert broker_service.store.get("USR-7", "dhan") is None


# --- the scheduled refresh --------------------------------------------------
def test_refresh_token_updates_the_record(broker_service, no_threads, dhan_ok,
                                          monkeypatch):
    broker_service.enable("USR-8", "dhan", DhanEnableRequest(**VALID))
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
    assert ("USR-8", "dhan") in broker_service.scheduler.scheduled


def test_startup_reconcile_repairs_interrupted_attempts(broker_service, no_threads,
                                                        dhan_ok):
    broker_service.enable("USR-9", "dhan", DhanEnableRequest(**VALID))
    # Simulate a restart mid-attempt: the record stayed "connecting".
    interrupted = broker_service.store.get("USR-9", "dhan")
    interrupted.status = "connecting"
    broker_service.store.put(interrupted)
    broker_service.reconcile_on_startup()
    record = broker_service.store.get("USR-9", "dhan")
    assert record.status == "failed"
    assert "interrupted" in record.last_error


def test_list_is_scoped_to_the_calling_user(broker_service, no_threads, dhan_ok):
    broker_service.enable("USR-A", "dhan", DhanEnableRequest(**VALID))
    broker_service.enable("USR-B", "dhan", DhanEnableRequest(**VALID))
    assert [b.broker for b in broker_service.list_for_user("USR-A").brokers] == ["dhan"]
    assert broker_service.list_for_user("USR-ZZ").brokers == []


def test_scheduler_job_id_is_stable():
    assert job_id("dhan", "USR-1") == "dhan-token-USR-1"


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

    broker_service.enable("USR-C1", "dhan", DhanEnableRequest(**VALID))
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

    broker_service.enable("USR-C2", "dhan", DhanEnableRequest(**VALID))

    def broken(key):
        raise SecretStoreError("parameter store down")

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
    broker.enable(user_id, "dhan", DhanEnableRequest(**VALID))

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


def test_secret_store_names_are_slash_prefixed(settings):
    """Parameter Store names must be canonical ``/path`` form: LocalStack's SSM
    refuses ``GetParameter`` for bare ``a/b`` names (ValidationException) while
    ``PutParameter`` silently accepts them — the read path would break with a
    validation error the first time a token is fetched. Real AWS accepts both,
    so always emit the slash-prefixed form."""
    from app.storage.secret_store import SecretStore

    store = SecretStore(settings)
    assert store._name("USR-1/dhan") == \
        f"/{settings.secrets_prefix}/USR-1/dhan"
    assert store._name("USR-1/dhan").startswith("/")


# --- Fyers -------------------------------------------------------------------
VALID_FYERS = dict(username="trader@fyers.in", client_id="AB12CD-100",
                   secret_key="app-secret", totp_key="JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
                   pin="1122")

from app.models.broker import FyersEnableRequest  # noqa: E402


@pytest.fixture
def fyers_steps(monkeypatch):
    """A URL-dispatching fake of Fyers's four-step login + code exchange.

    The steps' canned answers can be replaced per test via the returned dict;
    each entry is (status_code, json_body) or an Exception to raise. Every
    request body is recorded in ``FYERS_CALLS`` as (url, data) so tests can
    assert the exact wire format.
    """
    from app.services import broker_service as bs

    steps = {
        bs.FYERS_SEND_LOGIN_OTP_URL: (200, {"request_key": "rk-1"}),
        bs.FYERS_VERIFY_OTP_URL: (200, {"request_key": "rk-2"}),
        bs.FYERS_VERIFY_PIN_URL: (200, {"data": {"access_token": "pin-tok"}}),
        bs.FYERS_TOKEN_URL: (308, {"Url": "https://redirect/fyers?auth_code=AUTH-123"}),
        bs.FYERS_VALIDATE_AUTHCODE_URL: (200, {"access_token": "fy-tok",
                                               "refresh_token": "r-1", "s": "ok"}),
    }
    calls = []

    def fake(url, data, headers=None, timeout=10):
        calls.append((url, data))
        answer = steps[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(bs, "_fyers_request", fake)
    monkeypatch.setattr(bs, "FYERS_CALLS", calls, raising=False)
    return steps


def test_fyers_enable_connects_via_the_four_steps(broker_service, no_threads,
                                                  fyers_steps):
    import hashlib

    from app.services import broker_service as bs

    broker_service.enable("USR-F1", "fyers", FyersEnableRequest(**VALID_FYERS))
    record = broker_service.store.get("USR-F1", "fyers")
    assert record.status == "connected"
    # Credentials live only in the (fake) parameter store, keyed per broker…
    stored = broker_service.secrets.secrets["USR-F1/fyers"]
    assert stored["pin"] == "1122" and stored["secret_key"] == "app-secret"
    # …the token was stored separately…
    assert broker_service.secrets.secrets["USR-F1/fyers-token"]["access_token"] == "fy-tok"
    # …the raw exchange response is preserved for expiry reads…
    token_doc = broker_service.secrets.secrets["USR-F1/fyers-token"]
    assert token_doc["response"]["refresh_token"] == "r-1"
    # …and the daily refresh was armed for the fyers job.
    assert broker_service.scheduler.scheduled == [("USR-F1", "fyers")]

    # The exchange body matches fyers-apiv3's SessionModel.generate_token
    # byte-for-byte: the auth code rides in "code" (NOT "auth_code") and the
    # app-id hash is sha256(client_id:secret_key). (A wrong field name made
    # Fyers answer 400 once — see the regression history.)
    exchange_calls = [d for u, d in bs.FYERS_CALLS
                      if u == bs.FYERS_VALIDATE_AUTHCODE_URL]
    assert len(exchange_calls) == 1
    expected_hash = hashlib.sha256(
        f"{VALID_FYERS['client_id']}:{VALID_FYERS['secret_key']}".encode()
    ).hexdigest()
    assert '"grant_type":"authorization_code"' in exchange_calls[0]
    assert f'"appIdHash":"{expected_hash}"' in exchange_calls[0]
    assert '"code":"AUTH-123"' in exchange_calls[0]
    assert '"auth_code"' not in exchange_calls[0]
    assert '"response_type"' not in exchange_calls[0]


def test_fyers_totp_failure_waits_for_the_next_cycle(broker_service,
                                                     no_threads, fyers_steps,
                                                     monkeypatch):
    from app.services import broker_service as bs

    # The OTP verification step fails once (a TOTP window rolled over), then works.
    attempts = {"n": 0}
    real = fyers_steps[bs.FYERS_VERIFY_OTP_URL]
    fyers_steps[bs.FYERS_VERIFY_OTP_URL] = (400, {"message": "invalid otp"})
    waits: list = []

    import app.services.broker_service as svc_mod

    original_step2 = bs.BrokerService._fyers_login_step2

    def step2_once_failing(self, request_key, totp_key):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise bs.FyersRetryable("TOTP verification failed (HTTP 400)")
        fyers_steps[bs.FYERS_VERIFY_OTP_URL] = real
        return original_step2(self, request_key, totp_key)

    monkeypatch.setattr(bs.BrokerService, "_fyers_login_step2", step2_once_failing)
    monkeypatch.setattr(svc_mod, "_sleep", waits.append)

    broker_service.enable("USR-F2", "fyers", FyersEnableRequest(**VALID_FYERS))
    record = broker_service.store.get("USR-F2", "fyers")
    assert record.status == "connected"
    assert waits and 0 < waits[0] <= 31  # waited for the fresh TOTP window


def test_fyers_wrong_pin_is_a_hard_failure(broker_service, no_threads,
                                           fyers_steps):
    from app.services import broker_service as bs

    fyers_steps[bs.FYERS_VERIFY_PIN_URL] = (400, {"message": "invalid pin"})
    broker_service.enable("USR-F3", "fyers", FyersEnableRequest(**VALID_FYERS))
    record = broker_service.store.get("USR-F3", "fyers")
    assert record.status == "failed"
    assert "refused the PIN" in record.last_error
    # No daily job for a failed connection.
    assert broker_service.scheduler.scheduled == []


def test_fyers_auth_code_refusal_surfaces_the_real_reason(broker_service,
                                                          no_threads,
                                                          fyers_steps):
    """Regression: a hard failure inside the auth-code step was swallowed by
    the generic retry handler, so the UI showed the useless
    'request error (BrokerError)' and the actual reason (e.g. redirect-URI
    mismatch) never reached anyone. It must fail fast with Fyers's message."""
    from app.services import broker_service as bs

    fyers_steps[bs.FYERS_TOKEN_URL] = (
        400, {"message": "Invalid Redirect URL / invalid app details"})
    broker_service.enable("USR-F3b", "fyers", FyersEnableRequest(**VALID_FYERS))
    record = broker_service.store.get("USR-F3b", "fyers")
    assert record.status == "failed"
    assert "Invalid Redirect URL" in record.last_error
    assert "request error (BrokerError)" not in record.last_error
    assert broker_service.scheduler.scheduled == []


def test_fyers_enable_rejects_an_invalid_totp_key(broker_service):
    with pytest.raises(BrokerError):
        broker_service.enable("USR-F4", "fyers", FyersEnableRequest(
            **{**VALID_FYERS, "totp_key": "not-base32!!"}))
    assert broker_service.secrets.secrets == {}


def test_both_brokers_can_be_connected_at_once(broker_service, no_threads,
                                               dhan_ok, fyers_steps):
    broker_service.enable("USR-DUO", "dhan", DhanEnableRequest(**VALID))
    broker_service.enable("USR-DUO", "fyers", FyersEnableRequest(**VALID_FYERS))
    section = broker_service.runtime_credentials("USR-DUO")
    assert set(section) == {"dhan", "fyers"}
    assert section["dhan"]["access_token"] == "tok-123"
    assert section["fyers"]["access_token"] == "fy-tok"
    # Each is independently removable.
    broker_service.disable("USR-DUO", "dhan")
    assert set(broker_service.runtime_credentials("USR-DUO")) == {"fyers"}


def test_job_ids_are_distinct_per_broker():
    assert job_id("dhan", "USR-1") == "dhan-token-USR-1"
    assert job_id("fyers", "USR-1") == "fyers-token-USR-1"
    assert job_id("dhan", "USR-1") != job_id("fyers", "USR-1")
