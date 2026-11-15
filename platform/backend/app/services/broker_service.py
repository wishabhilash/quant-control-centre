"""Broker connections: enable, connect, disable and the daily token refresh.

A user connects a broker (currently Dhan) once. The platform stores the
credentials in **AWS Parameter Store (SSM)** — never in an S3 document or a strategy
project (golden rule 7) — then generates an access token using the TOTP-based
flow and re-generates it daily at ``BROKER_TOKEN_HOUR`` (default 08:00 IST)
before the market opens, via the in-process APScheduler (SQLite job store).

The Dhan token protocol (as documented by the flow the platform implements):
``POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=..&pin=..&totp=..``
with retries while Dhan answers ``{"status": "error", "message": "Invalid TOTP"}`` —
each retry waits for the next 30-second TOTP cycle, since the token that was
generated may have expired between building the request and Dhan reading it.

Secrets hygiene: neither the PIN, the TOTP secret nor the access token is ever
logged; the client id appears only masked. Request errors are logged with
their *type* only — a connection error's text can contain the full URL, which
carries the PIN.
"""
from __future__ import annotations

import datetime as _dt
import threading
from typing import List, Optional, Tuple

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.broker import (
    BrokerDocument,
    BrokerInfo,
    BrokerListResponse,
    DhanEnableRequest,
    DhanSecret,
    DhanToken,
)
from app.storage.broker_store import BrokerStore
from app.storage.secret_store import SecretStore, SecretStoreError

log = get_logger("services.broker")

DHAN_AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"


class BrokerError(Exception):
    """User-facing broker failure."""


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _sleep(seconds: float) -> None:
    """Indirection so tests can fast-forward the TOTP-cycle waits."""
    import time

    time.sleep(seconds)


def _dhan_request(url: str, timeout: int = 10) -> Tuple[int, dict]:
    """The single HTTP touchpoint with Dhan (isolated for tests)."""
    import requests

    response = requests.post(url, timeout=timeout)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body


def _seconds_to_next_totp_cycle() -> float:
    """How long until the current 30-second TOTP window rolls over."""
    import time

    return 30 - (int(time.time()) % 30)


class BrokerService:
    def __init__(self, settings: Optional[Settings] = None,
                 store: Optional[BrokerStore] = None,
                 secrets: Optional[SecretStore] = None,
                 scheduler=None):
        self.settings = settings or get_settings()
        self.store = store or BrokerStore(self.settings)
        self.secrets = secrets or SecretStore(self.settings)
        self.scheduler = scheduler  # BrokerScheduler (or a test spy)
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

    # --- user-facing --------------------------------------------------------
    def list_for_user(self, user_id: str) -> BrokerListResponse:
        brokers = [
            self.to_info(doc, next_run=self._next_refresh(user_id, doc))
            for doc in self.store.list_for_user(user_id)
        ]
        return BrokerListResponse(brokers=brokers)

    def enable(self, user_id: str, req: DhanEnableRequest) -> BrokerDocument:
        """Store credentials, then attempt to connect in the background.

        Returns immediately with ``status=connecting``; the dashboard polls
        until it flips to ``connected`` or ``failed`` (the TOTP retries can
        legitimately take ~90 s — three 30-second cycles).
        """
        creds = DhanSecret(
            client_id=(req.client_id or "").strip(),
            pin=(req.pin or "").strip(),
            totp_secret=(req.totp_secret or "").strip().replace(" ", ""),
        )
        if not creds.client_id or not creds.pin or not creds.totp_secret:
            raise BrokerError("client id, PIN and TOTP secret are all required")
        # Validate the TOTP secret BEFORE storing anything: a typo should not
        # leave credentials in Parameter Store (SSM).
        self._validate_totp_secret(creds.totp_secret)

        self.secrets.put_secret(self._creds_key(user_id), creds.model_dump())
        doc = BrokerDocument(
            user_id=user_id, broker="dhan", status="connecting",
            enabled_at=_now_iso(), last_token_at=None, last_error=None,
        )
        self.store.put(doc)
        log.info("broker dhan enable requested for %s (client %s)",
                 user_id, _mask(creds.client_id))
        self._spawn_attempt(user_id)
        return doc

    def retry(self, user_id: str) -> BrokerDocument:
        """Re-attempt a failed connection using the stored credentials."""
        doc = self.store.get(user_id, "dhan")
        if doc is None:
            raise BrokerError("Dhan is not connected for this account")
        if self.secrets.get_secret(self._creds_key(user_id)) is None:
            raise BrokerError("the stored credentials are missing — re-enable Dhan")
        doc.status = "connecting"
        doc.last_error = None
        self.store.put(doc)
        self._spawn_attempt(user_id)
        return doc

    def disable(self, user_id: str, broker: str = "dhan") -> None:
        """Remove the connection: credentials, token, schedule and record."""
        if broker != "dhan":
            raise BrokerError(f"unsupported broker: {broker}")
        self.secrets.delete_secret(self._creds_key(user_id))
        self.secrets.delete_secret(self._token_key(user_id))
        if self.scheduler is not None:
            self.scheduler.unschedule(user_id)
        self.store.delete(user_id, broker)
        log.info("broker dhan disabled for %s", user_id)

    # --- the scheduled job --------------------------------------------------
    def refresh_token(self, user_id: str) -> None:
        """Regenerate the access token (the 08:00 job body).

        A failure does NOT disconnect the user — the credentials remain valid,
        so the record keeps its ``connected`` status and the error is surfaced
        for the next day's run (and in the UI).
        """
        doc = self.store.get(user_id, "dhan")
        if doc is None:
            log.warning("token refresh for %s: no connection record", user_id)
            return
        creds = self.secrets.get_secret(self._creds_key(user_id))
        if creds is None:
            log.error("token refresh for %s: credentials missing", user_id)
            doc.last_error = "credentials missing"
            self.store.put(doc)
            return
        try:
            token = self.generate_dhan_token(DhanSecret.model_validate(creds))
        except BrokerError as exc:
            log.error("token refresh for %s failed: %s", user_id, exc)
            doc.last_error = str(exc)
            self.store.put(doc)
            if self.scheduler is not None:
                self.scheduler.schedule_daily(user_id)  # keep tomorrow's run
            return
        self._store_token(user_id, token)
        doc.status = "connected"
        doc.last_token_at = _now_iso()
        doc.last_error = None
        self.store.put(doc)
        log.info("dhan token refreshed for %s", user_id)

    # --- startup reconcile --------------------------------------------------
    def reconcile_on_startup(self) -> None:
        """Heal state after a restart: re-register jobs for connected users;
        fail connections whose in-flight attempt died with the old process."""
        for doc in self.store.list_all():
            if doc.status == "connected" and self.scheduler is not None:
                self.scheduler.schedule_daily(doc.user_id)
            elif doc.status == "connecting":
                doc.status = "failed"
                doc.last_error = "connection attempt interrupted by a restart"
                self.store.put(doc)
                log.warning("connection attempt for %s was interrupted", doc.user_id)

    # --- connection machinery (runs in a background thread) -----------------
    def _spawn_attempt(self, user_id: str) -> None:
        with self._inflight_lock:
            if user_id in self._inflight:
                return  # an attempt is already running
            self._inflight.add(user_id)
        thread = threading.Thread(target=self._attempt, args=(user_id,),
                                  name=f"dhan-connect-{user_id}", daemon=True)
        thread.start()

    def _attempt(self, user_id: str) -> None:
        try:
            creds = self.secrets.get_secret(self._creds_key(user_id))
            if creds is None:
                raise BrokerError("credentials were not stored")
            token = self.generate_dhan_token(DhanSecret.model_validate(creds))
            self._store_token(user_id, token)
            doc = self.store.get(user_id, "dhan") or BrokerDocument(
                user_id=user_id, broker="dhan", status="connecting")
            doc.status = "connected"
            doc.last_token_at = _now_iso()
            doc.last_error = None
            self.store.put(doc)
            if self.scheduler is not None:
                self.scheduler.schedule_daily(user_id)
            log.info("broker dhan connected for %s", user_id)
        except (BrokerError, SecretStoreError) as exc:
            doc = self.store.get(user_id, "dhan") or BrokerDocument(
                user_id=user_id, broker="dhan", status="connecting")
            doc.status = "failed"
            doc.last_error = str(exc)
            self.store.put(doc)
            log.error("broker dhan connection failed for %s: %s", user_id, exc)
        finally:
            with self._inflight_lock:
                self._inflight.discard(user_id)

    # --- the Dhan token protocol -------------------------------------------
    def _validate_totp_secret(self, secret: str) -> None:
        try:
            import pyotp

            pyotp.TOTP(secret).now()
        except Exception as exc:  # noqa: BLE001 — any decode failure is a typo
            raise BrokerError("the TOTP secret is not valid (expected a base32 "
                              "string from the authenticator setup)") from exc

    def generate_dhan_token(self, creds: DhanSecret) -> DhanToken:
        """Generate an access token, retrying while Dhan says ``Invalid TOTP``.

        A TOTP code is valid for 30 seconds; if the request is built just
        before the window rolls over, Dhan may read the *next* code. Waiting
        for the fresh cycle and retrying is the documented cure.
        """
        max_attempts = max(1, self.settings.broker_connect_attempts)
        last_error = ""
        for attempt in range(1, max_attempts + 1):
            import pyotp

            current_totp = pyotp.TOTP(creds.totp_secret).now()
            url = (f"{DHAN_AUTH_URL}?dhanClientId={creds.client_id}"
                   f"&pin={creds.pin}&totp={current_totp}")
            try:
                status_code, res_json = _dhan_request(url)
            except Exception as exc:  # noqa: BLE001 — logged WITHOUT the URL
                last_error = f"could not reach Dhan ({type(exc).__name__})"
                log.warning("attempt %s: request error (%s)", attempt,
                            type(exc).__name__)
                _sleep(2)
                continue

            if (res_json.get("status") == "error"
                    and res_json.get("message") == "Invalid TOTP"):
                last_error = "invalid TOTP"
                log.warning("attempt %s: Dhan rejected the TOTP — waiting for "
                            "the next cycle", attempt)
                if attempt < max_attempts:
                    _sleep(_seconds_to_next_totp_cycle() + 1)
                continue

            if status_code == 200 and res_json.get("status") != "error":
                access_token = (res_json.get("accessToken")
                                or res_json.get("access_token") or "")
                if not access_token:
                    raise BrokerError("Dhan's response carried no access token")
                log.info("dhan access token generated (client %s)",
                         _mask(creds.client_id))
                return DhanToken(access_token=access_token,
                                 generated_at=_now_iso(), response=res_json)

            # Any other answer is a hard failure — retrying won't help.
            message = res_json.get("message") or f"HTTP {status_code}"
            raise BrokerError(f"Dhan refused the request: {message}")
        raise BrokerError(
            f"could not generate the Dhan token after {max_attempts} attempts "
            f"({last_error or 'unknown error'})")

    # --- the strategy contract ----------------------------------------------
    def runtime_credentials(self, user_id: Optional[str]) -> dict:
        """Broker credentials for a strategy run, keyed by broker name.

        The runner calls this on every launch and writes the result into the
        runtime config, so a strategy reads
        ``config["brokers"]["dhan"]["access_token"]``. Returns ``{}`` when the
        user has no connected broker — and, deliberately, on ANY storage or
        secrets failure: a broken Parameter Store (SSM) must stop the *token*, not
        the strategy run.
        """
        if not user_id:
            return {}
        try:
            doc = self.store.get(user_id, "dhan")
            if doc is None or doc.status != "connected":
                return {}
            token = self.secrets.get_secret(self._token_key(user_id))
            if not token or not token.get("access_token"):
                return {}
            creds = self.secrets.get_secret(self._creds_key(user_id)) or {}
        except Exception as exc:  # noqa: BLE001 — never break a run over this
            log.warning("broker credentials unavailable for %s: %s", user_id,
                        type(exc).__name__)
            return {}
        return {"dhan": {
            "client_id": creds.get("client_id", ""),
            "access_token": token["access_token"],
            "generated_at": token.get("generated_at"),
        }}

    # --- helpers ------------------------------------------------------------
    def _store_token(self, user_id: str, token: DhanToken) -> None:
        self.secrets.put_secret(self._token_key(user_id), token.model_dump())

    def _creds_key(self, user_id: str) -> str:
        return f"{user_id}/dhan"

    def _token_key(self, user_id: str) -> str:
        return f"{user_id}/dhan-token"

    def _next_refresh(self, user_id: str, doc: BrokerDocument) -> Optional[str]:
        if doc.status != "connected" or self.scheduler is None:
            return None
        try:
            return self.scheduler.next_run(user_id)
        except Exception:  # noqa: BLE001 — the view must not break on the scheduler
            return None

    def to_info(self, doc: BrokerDocument, next_run: Optional[str] = None) -> BrokerInfo:
        return BrokerInfo(
            broker=doc.broker, status=doc.status, enabled_at=doc.enabled_at,
            last_token_at=doc.last_token_at, last_error=doc.last_error,
            next_refresh_at=next_run,
        )

    def list_connections(self, user_id: str) -> List[BrokerDocument]:
        return self.store.list_for_user(user_id)


def _mask(client_id: str) -> str:
    return (client_id[:4] + "…") if len(client_id) > 4 else "…"
