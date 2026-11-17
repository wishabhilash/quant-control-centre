"""Broker connections: enable, connect, disable and the daily token refresh.

A user connects a broker once. The platform stores the credentials in
**AWS Parameter Store (SSM)** — never in an S3 document or a strategy project
(golden rule 7) — then generates an access token using the broker's flow and
re-generates it daily at ``BROKER_TOKEN_HOUR`` (default 08:00 IST) before the
market opens, via the in-process APScheduler (SQLite job store).

This service is **broker-agnostic orchestration only**: the connection
lifecycle, the background attempt thread, the SSM key layout and the
strategy-facing ``runtime_credentials`` contract. Every protocol detail —
URLs, request shapes, retry rules — lives in a per-broker adapter under
``app/brokers/`` (registered in ``ADAPTERS``), so adding a broker never
touches this file.

Secrets hygiene: neither the PIN, the TOTP key, the app secret nor the access
token is ever logged; the client id appears only masked. Request errors are
logged with their *type* only — an error body can contain credentials.
"""
from __future__ import annotations

import threading
from typing import Any, List, Optional

from app.brokers import SUPPORTED_BROKERS, get_adapter
from app.brokers.base import BrokerAdapter, BrokerError, _mask, _now_iso
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.broker import (
    BrokerDocument,
    BrokerInfo,
    BrokerListResponse,
)
from app.storage.broker_store import BrokerStore
from app.storage.secret_store import SecretStore, SecretStoreError

log = get_logger("services.broker")


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
            self.to_info(doc, next_run=self._next_refresh(doc))
            for doc in self.store.list_for_user(user_id)
        ]
        return BrokerListResponse(brokers=brokers)

    def enable(self, user_id: str, broker: str, req: Any) -> BrokerDocument:
        """Store credentials, then attempt to connect in the background.

        Returns immediately with ``status=connecting``; the dashboard polls
        until it flips to ``connected`` or ``failed`` (the TOTP retries can
        legitimately take ~90 s — three 30-second cycles).
        """
        adapter = get_adapter(broker)  # BrokerError for an unknown name
        # The adapter validates + trims, refusing missing fields BEFORE
        # anything is stored (a typo should not leave credentials in SSM).
        creds: dict = adapter.normalize(req)
        self._validate_totp_secret(adapter.totp_key(creds))

        self.secrets.put_secret(self._creds_key(user_id, broker), creds)
        doc = BrokerDocument(
            user_id=user_id, broker=broker, status="connecting",
            enabled_at=_now_iso(), last_token_at=None, last_error=None,
        )
        self.store.put(doc)
        log.info("broker %s enable requested for %s (client %s)",
                 broker, user_id, _mask(creds.get("client_id", "")))
        self._spawn_attempt(user_id, broker)
        return doc

    def retry(self, user_id: str, broker: str = "dhan") -> BrokerDocument:
        """Re-attempt a failed connection using the stored credentials."""
        doc = self.store.get(user_id, broker)
        if doc is None:
            raise BrokerError(f"{broker} is not connected for this account")
        if self.secrets.get_secret(self._creds_key(user_id, broker)) is None:
            raise BrokerError(
                f"the stored credentials are missing — re-enable {broker}")
        doc.status = "connecting"
        doc.last_error = None
        self.store.put(doc)
        self._spawn_attempt(user_id, broker)
        return doc

    def disable(self, user_id: str, broker: str = "dhan") -> None:
        """Remove the connection: credentials, token, schedule and record."""
        if broker not in SUPPORTED_BROKERS:
            raise BrokerError(f"unsupported broker: {broker}")
        self.secrets.delete_secret(self._creds_key(user_id, broker))
        self.secrets.delete_secret(self._token_key(user_id, broker))
        if self.scheduler is not None:
            self.scheduler.unschedule(user_id, broker)
        self.store.delete(user_id, broker)
        log.info("broker %s disabled for %s", broker, user_id)

    # --- the scheduled job --------------------------------------------------
    def refresh_token(self, user_id: str, broker: str = "dhan") -> None:
        """Regenerate the access token (the 08:00 job body).

        A failure does NOT disconnect the user — the credentials remain valid,
        so the record keeps its ``connected`` status and the error is surfaced
        for the next day's run (and in the UI).
        """
        doc = self.store.get(user_id, broker)
        if doc is None:
            log.warning("token refresh for %s/%s: no connection record",
                        user_id, broker)
            return
        creds = self.secrets.get_secret(self._creds_key(user_id, broker))
        if creds is None:
            log.error("token refresh for %s/%s: credentials missing",
                      user_id, broker)
            doc.last_error = "credentials missing"
            self.store.put(doc)
            return
        try:
            token = self.adapter(broker).generate_token(creds, self.settings)
        except BrokerError as exc:
            log.error("token refresh for %s/%s failed: %s", user_id, broker, exc)
            doc.last_error = str(exc)
            self.store.put(doc)
            if self.scheduler is not None:
                self.scheduler.schedule_daily(user_id, broker)  # tomorrow too
            return
        self._store_token(user_id, broker, token)
        doc.status = "connected"
        doc.last_token_at = _now_iso()
        doc.last_error = None
        self.store.put(doc)
        log.info("%s token refreshed for %s", broker, user_id)

    # --- startup reconcile --------------------------------------------------
    def reconcile_on_startup(self) -> None:
        """Heal state after a restart: re-register jobs for connected users;
        fail connections whose in-flight attempt died with the old process."""
        for doc in self.store.list_all():
            if doc.status == "connected" and self.scheduler is not None:
                self.scheduler.schedule_daily(doc.user_id, doc.broker)
            elif doc.status == "connecting":
                doc.status = "failed"
                doc.last_error = "connection attempt interrupted by a restart"
                self.store.put(doc)
                log.warning("connection attempt for %s/%s was interrupted",
                            doc.user_id, doc.broker)

    # --- connection machinery (runs in a background thread) -----------------
    def _spawn_attempt(self, user_id: str, broker: str) -> None:
        key = f"{user_id}:{broker}"
        with self._inflight_lock:
            if key in self._inflight:
                return  # an attempt is already running
            self._inflight.add(key)
        thread = threading.Thread(target=self._attempt, args=(user_id, broker),
                                  name=f"{broker}-connect-{user_id}",
                                  daemon=True)
        thread.start()

    def _attempt(self, user_id: str, broker: str = "dhan") -> None:
        try:
            creds = self.secrets.get_secret(self._creds_key(user_id, broker))
            if creds is None:
                raise BrokerError("credentials were not stored")
            token = self.adapter(broker).generate_token(creds, self.settings)
            self._store_token(user_id, broker, token)
            doc = self.store.get(user_id, broker) or BrokerDocument(
                user_id=user_id, broker=broker, status="connecting")
            doc.status = "connected"
            doc.last_token_at = _now_iso()
            doc.last_error = None
            self.store.put(doc)
            if self.scheduler is not None:
                self.scheduler.schedule_daily(user_id, broker)
            log.info("broker %s connected for %s", broker, user_id)
        except (BrokerError, SecretStoreError) as exc:
            doc = self.store.get(user_id, broker) or BrokerDocument(
                user_id=user_id, broker=broker, status="connecting")
            doc.status = "failed"
            doc.last_error = str(exc)
            self.store.put(doc)
            log.error("broker %s connection failed for %s: %s",
                      broker, user_id, exc)
        finally:
            with self._inflight_lock:
                self._inflight.discard(f"{user_id}:{broker}")

    # --- the strategy contract ----------------------------------------------
    def runtime_credentials(self, user_id: Optional[str]) -> dict:
        """Broker credentials for a strategy run, keyed by broker name.

        The runner calls this on every launch and writes the result into the
        runtime config, so a strategy reads
        ``config["brokers"]["<broker>"]["access_token"]``. Returns ``{}`` when
        the user has no connected broker — and, deliberately, on ANY storage
        or secrets failure: a broken Parameter Store (SSM) must stop the
        *token*, not the strategy run. One broker's failure never removes
        another's credentials.
        """
        if not user_id:
            return {}
        section: dict = {}
        for broker in SUPPORTED_BROKERS:
            try:
                doc = self.store.get(user_id, broker)
                if doc is None or doc.status != "connected":
                    continue
                token = self.secrets.get_secret(self._token_key(user_id, broker))
                if not token or not token.get("access_token"):
                    continue
                creds = self.secrets.get_secret(
                    self._creds_key(user_id, broker)) or {}
            except Exception as exc:  # noqa: BLE001 — skip this broker only
                log.warning("broker credentials unavailable for %s/%s: %s",
                            user_id, broker, type(exc).__name__)
                continue
            section[broker] = {
                "client_id": creds.get("client_id", ""),
                "access_token": token["access_token"],
                "generated_at": token.get("generated_at"),
            }
        return section

    # --- helpers ------------------------------------------------------------
    @staticmethod
    def adapter(broker: str) -> BrokerAdapter:
        """The broker's adapter (registry lookup; BrokerError if unknown)."""
        return get_adapter(broker)

    def _store_token(self, user_id: str, broker: str, token) -> None:
        self.secrets.put_secret(self._token_key(user_id, broker),
                                token.model_dump())

    def _creds_key(self, user_id: str, broker: str) -> str:
        return f"{user_id}/{broker}"

    def _token_key(self, user_id: str, broker: str) -> str:
        return f"{user_id}/{broker}-token"

    def _validate_totp_secret(self, secret: str) -> None:
        try:
            import pyotp

            pyotp.TOTP(secret).now()
        except Exception as exc:  # noqa: BLE001 — any decode failure is a typo
            raise BrokerError("the TOTP secret is not valid (expected a base32 "
                              "string from the authenticator setup)") from exc

    def _next_refresh(self, doc: BrokerDocument) -> Optional[str]:
        if doc.status != "connected" or self.scheduler is None:
            return None
        try:
            return self.scheduler.next_run(doc.user_id, doc.broker)
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
