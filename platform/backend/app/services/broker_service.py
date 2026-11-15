"""Broker connections: enable, connect, disable and the daily token refresh.

A user connects a broker (currently **Dhan** or **Fyers**) once. The platform
stores the credentials in **AWS Parameter Store (SSM)** — never in an S3
document or a strategy project (golden rule 7) — then generates an access
token using the broker's flow and re-generates it daily at
``BROKER_TOKEN_HOUR`` (default 08:00 IST) before the market opens, via the
in-process APScheduler (SQLite job store).

The Dhan token protocol: ``POST https://auth.dhan.co/app/generateAccessToken
?dhanClientId=..&pin=..&totp=..`` with retries while Dhan answers
``{"status": "error", "message": "Invalid TOTP"}`` — each retry waits for the
next 30-second TOTP cycle, since the token that was generated may have expired
between building the request and Dhan reading it.

The Fyers token protocol (the documented headless login, four steps): send a
login OTP to the Fyers id → verify the TOTP → verify the PIN → request an
auth code for the registered app (HTTP 308 with a URL carrying
``auth_code``), then exchange the auth code at ``/api/v3/validate-authcode``
with ``appIdHash = sha256(client_id:secret_key)`` for the access token.
Retryable failures are the OTP/TOPT steps (a fresh TOTP window can cure them);
a wrong PIN or an app-level refusal is a hard failure.

Secrets hygiene: neither the PIN, the TOTP key, the app secret nor the access
token is ever logged; the client id appears only masked. Request errors are
logged with their *type* only — an error body can contain credentials.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import threading
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.broker import (
    BrokerDocument,
    BrokerInfo,
    BrokerListResponse,
    DhanEnableRequest,
    DhanSecret,
    DhanToken,
    FyersEnableRequest,
    FyersSecret,
    FyersToken,
)
from app.storage.broker_store import BrokerStore
from app.storage.secret_store import SecretStore, SecretStoreError

log = get_logger("services.broker")

DHAN_AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"

FYERS_SEND_LOGIN_OTP_URL = "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2"
FYERS_VERIFY_OTP_URL = "https://api-t2.fyers.in/vagator/v2/verify_otp"
FYERS_VERIFY_PIN_URL = "https://api-t2.fyers.in/vagator/v2/verify_pin_v2"
FYERS_TOKEN_URL = "https://api-t1.fyers.in/api/v3/token"
FYERS_VALIDATE_AUTHCODE_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"

# Every broker the platform knows about, in UI order. The Fyers redirect URI is
# fixed: the headless login uses the app's registered redirect and the auth
# code is read from the 308 response, never followed.
SUPPORTED_BROKERS = ("dhan", "fyers")
FYERS_REDIRECT_URI = "http://127.0.0.1:8080"


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


def _fyers_request(url: str, data: str, headers: Optional[dict] = None,
                   timeout: int = 10) -> Tuple[int, dict]:
    """The single HTTP touchpoint with Fyers (isolated for tests).

    Redirects are never followed: Fyers answers the auth-code step with a
    **308** whose URL comes in the *body* — following a Location header (if
    one ever appeared) would hit the local redirect URI and lose the answer.
    An unexpected 3xx surfaces as an error instead of being chased.
    """
    import requests

    response = requests.post(url, data=data, headers=headers, timeout=timeout,
                             allow_redirects=False)
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
            self.to_info(doc, next_run=self._next_refresh(doc))
            for doc in self.store.list_for_user(user_id)
        ]
        return BrokerListResponse(brokers=brokers)

    def enable(self, user_id: str, broker: str,
               req: "DhanEnableRequest | FyersEnableRequest") -> BrokerDocument:
        """Store credentials, then attempt to connect in the background.

        Returns immediately with ``status=connecting``; the dashboard polls
        until it flips to ``connected`` or ``failed`` (the TOTP retries can
        legitimately take ~90 s — three 30-second cycles).
        """
        if broker == "dhan":
            creds: "DhanSecret | FyersSecret" = DhanSecret(
                client_id=(req.client_id or "").strip(),
                pin=(req.pin or "").strip(),
                totp_secret=(req.totp_secret or "").strip().replace(" ", ""),
            )
            totp_key = creds.totp_secret
            missing = [f for f, v in (("client id", creds.client_id),
                                      ("PIN", creds.pin),
                                      ("TOTP secret", creds.totp_secret))
                       if not v]
        elif broker == "fyers":
            creds = FyersSecret(
                username=(req.username or "").strip(),
                client_id=(req.client_id or "").strip(),
                secret_key=(req.secret_key or "").strip(),
                totp_key=(req.totp_key or "").strip().replace(" ", ""),
                pin=(req.pin or "").strip(),
            )
            totp_key = creds.totp_key
            missing = [f for f, v in (("Fyers username", creds.username),
                                      ("client id", creds.client_id),
                                      ("secret key", creds.secret_key),
                                      ("TOTP key", creds.totp_key),
                                      ("PIN", creds.pin))
                       if not v]
        else:
            raise BrokerError(f"unsupported broker: {broker}")
        if missing:
            raise BrokerError(f"{', '.join(missing)} required for {broker}")
        # Validate the TOTP key BEFORE storing anything: a typo should not
        # leave credentials in Parameter Store (SSM).
        self._validate_totp_secret(totp_key)

        self.secrets.put_secret(self._creds_key(user_id, broker),
                                creds.model_dump())
        doc = BrokerDocument(
            user_id=user_id, broker=broker, status="connecting",
            enabled_at=_now_iso(), last_token_at=None, last_error=None,
        )
        self.store.put(doc)
        log.info("broker %s enable requested for %s (client %s)",
                 broker, user_id, _mask(creds.client_id))
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
            token = self._generate_token(broker, creds)
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
            token = self._generate_token(broker, creds)
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

    # --- token generation (per broker) --------------------------------------
    def _generate_token(self, broker: str, creds: dict) -> "DhanToken | FyersToken":
        """Dispatch to the broker's token protocol."""
        if broker == "dhan":
            return self.generate_dhan_token(DhanSecret.model_validate(creds))
        if broker == "fyers":
            return self.generate_fyers_token(FyersSecret.model_validate(creds))
        raise BrokerError(f"unsupported broker: {broker}")

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

    def generate_fyers_token(self, creds: FyersSecret) -> FyersToken:
        """Generate a Fyers access token (the four-step headless login).

        Steps: (1) send a login OTP to the Fyers id, (2) verify the TOTP,
        (3) verify the PIN, (4) request an auth code for the app — Fyers
        answers **308** with a URL whose query carries ``auth_code`` — then
        exchange it at ``/api/v3/validate-authcode`` with
        ``appIdHash = sha256(client_id:secret_key)``.

        Retrying mirrors the Dhan protocol: the OTP steps can legitimately
        fail while the TOTP window rolls over, so those get a fresh cycle;
        a wrong PIN or an app refusal is a hard failure (retrying won't help).
        """
        import pyotp

        max_attempts = max(1, self.settings.broker_connect_attempts)
        last_error = ""
        for attempt in range(1, max_attempts + 1):
            # Steps 1–3: the login chain (one requests session per attempt).
            try:
                request_key = self._fyers_login_step1(creds)
                request_key = self._fyers_login_step2(request_key,
                                                      creds.totp_key)
                pin_access_token = self._fyers_login_step3(request_key,
                                                           creds.pin)
            except FyersRetryable as exc:
                last_error = str(exc)
                log.warning("attempt %s: Fyers login step needs a fresh TOTP "
                            "window (%s)", attempt, type(exc).__name__)
                if attempt < max_attempts:
                    _sleep(_seconds_to_next_totp_cycle() + 1)
                continue
            except BrokerError:
                raise  # hard failure — retrying won't help
            except Exception as exc:  # noqa: BLE001 — logged WITHOUT details
                last_error = f"request error ({type(exc).__name__})"
                log.warning("attempt %s: request error (%s)", attempt,
                            type(exc).__name__)
                _sleep(2)
                continue

            try:
                # Step 4: request the auth code for the registered app.
                auth_code = self._fyers_auth_code(pin_access_token, creds)
                # Step 5: exchange it for the access token (response verbatim).
                exchange = self._fyers_exchange_authcode(auth_code, creds)
            except BrokerError:
                # A hard failure (refused request / no auth_code) must NOT be
                # swallowed into the retry loop: retrying won't help, and the
                # message carries the actual reason (HTTP status + Fyers's
                # message) the user needs to see.
                raise
            except Exception as exc:  # noqa: BLE001 — logged WITHOUT details
                last_error = f"request error ({type(exc).__name__})"
                log.warning("attempt %s: request error (%s)", attempt,
                            type(exc).__name__)
                _sleep(2)
                continue

            access_token = exchange.get("access_token") or ""
            if not access_token:
                raise BrokerError("Fyers's response carried no access token")
            log.info("fyers access token generated (client %s)",
                     _mask(creds.client_id))
            return FyersToken(access_token=access_token,
                              generated_at=_now_iso(),
                              response=exchange)

        raise BrokerError(
            f"could not generate the Fyers token after {max_attempts} attempts "
            f"({last_error or 'unknown error'})")

    def _fyers_login_step1(self, creds: FyersSecret) -> str:
        """Send the login OTP to the Fyers id → ``request_key``."""
        data = ('{"fy_id":"%s","app_id":"2"}' % _b64(creds.username))
        status_code, body = _fyers_request(FYERS_SEND_LOGIN_OTP_URL, data)
        if status_code != 200 or body.get("request_key") is None:
            raise FyersRetryable(f"login OTP refused (HTTP {status_code})")
        return str(body["request_key"])

    def _fyers_login_step2(self, request_key: str, totp_key: str) -> str:
        """Verify the TOTP → the next ``request_key``."""
        import pyotp

        data = '{"request_key":"%s","otp":%s}' % (request_key,
                                                  pyotp.TOTP(totp_key).now())
        status_code, body = _fyers_request(FYERS_VERIFY_OTP_URL, data)
        if status_code != 200 or body.get("request_key") is None:
            raise FyersRetryable(f"TOTP verification failed (HTTP {status_code})")
        return str(body["request_key"])

    def _fyers_login_step3(self, request_key: str, pin: str) -> str:
        """Verify the PIN → a short-lived pin access token."""
        data = ('{"request_key":"%s","identity_type":"pin","identifier":"%s"}'
                % (request_key, _b64(pin)))
        status_code, body = _fyers_request(FYERS_VERIFY_PIN_URL, data)
        access_token = ((body.get("data") or {}).get("access_token")
                        if isinstance(body, dict) else None)
        if status_code != 200 or not access_token:
            # A wrong PIN is a hard failure — no amount of retrying helps.
            raise BrokerError(
                f"Fyers refused the PIN (HTTP {status_code}, "
                f"{str(_reason(body))[:120]})")
        return str(access_token)

    def _fyers_auth_code(self, pin_access_token: str,
                         creds: FyersSecret) -> str:
        """Request the app auth code — Fyers answers **308** with a URL."""
        data = ('{"fyers_id":"%s","app_id":"%s","redirect_uri":"%s",'
                '"appType":"100","code_challenge":"","state":"abcdefg",'
                '"scope":"","nonce":"","response_type":"code",'
                '"create_cookie":true}'
                % (creds.username, creds.client_id[:-4], FYERS_REDIRECT_URI))
        headers = {"authorization": f"Bearer {pin_access_token}",
                   "content-type": "application/json; charset=UTF-8"}
        status_code, body = _fyers_request(FYERS_TOKEN_URL, data, headers)
        if status_code != 308:
            raise BrokerError(
                f"Fyers refused the auth-code request "
                f"(HTTP {status_code}, {str(_reason(body))[:120]})")
        url = (body.get("Url") or body.get("url") or "")
        query = parse_qs(urlparse(url).query)
        auth_code = (query.get("auth_code") or [None])[0]
        if not auth_code:
            raise BrokerError("Fyers's 308 response carried no auth_code")
        return auth_code

    def _fyers_exchange_authcode(self, auth_code: str,
                                 creds: FyersSecret) -> dict:
        """Exchange the auth code for the access token.

        Byte-for-byte what ``fyersModel.SessionModel.generate_token()`` of
        fyers-apiv3 sends (checked against the SDK's 3.2.1 source): the body is
        ``{"grant_type": "authorization_code", "appIdHash":
        sha256(client_id:secret_key), "code": auth_code}`` sent as JSON — note
        the field is **``code``**, and there is no ``response_type``. Done with
        a plain request so the *platform* venv needs no broker SDK (a strategy
        installs its own). Returns Fyers's response verbatim (``access_token``,
        ``refresh_token``, …) so a strategy can read expiry fields without the
        platform knowing them all.
        """
        app_id_hash = hashlib.sha256(
            f"{creds.client_id}:{creds.secret_key}".encode()).hexdigest()
        data = ('{"grant_type":"authorization_code","appIdHash":"%s",'
                '"code":"%s"}' % (app_id_hash, auth_code))
        headers = {"content-type": "application/json; charset=UTF-8"}
        status_code, body = _fyers_request(FYERS_VALIDATE_AUTHCODE_URL, data,
                                           headers)
        access_token = body.get("access_token") if isinstance(body, dict) else None
        if status_code != 200 or not access_token:
            raise BrokerError(
                f"Fyers refused the auth-code exchange (HTTP {status_code}, "
                f"{str(_reason(body))[:120]})")
        return body

    # --- the strategy contract ----------------------------------------------
    def runtime_credentials(self, user_id: Optional[str]) -> dict:
        """Broker credentials for a strategy run, keyed by broker name.

        The runner calls this on every launch and writes the result into the
        runtime config, so a strategy reads
        ``config["brokers"]["dhan"]["access_token"]`` or
        ``config["brokers"]["fyers"]["access_token"]``. Returns ``{}`` when
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
    def _store_token(self, user_id: str, broker: str, token) -> None:
        self.secrets.put_secret(self._token_key(user_id, broker),
                                token.model_dump())

    def _creds_key(self, user_id: str, broker: str) -> str:
        return f"{user_id}/{broker}"

    def _token_key(self, user_id: str, broker: str) -> str:
        return f"{user_id}/{broker}-token"

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


class FyersRetryable(Exception):
    """An OTP/TOTP-step failure a fresh TOTP window can cure."""


def _reason(body: object) -> str:
    """The human-readable reason Fyers put in an error body (never a secret —
    their error bodies carry messages, not credentials)."""
    if isinstance(body, dict):
        for key in ("message", "detail", "error", "s"):
            value = body.get(key)
            if value:
                return str(value)
    return "no reason given"


def _b64(text: str) -> str:
    """Fyers expects the fy_id / PIN as base64 in its JSON bodies."""
    import base64

    return base64.b64encode(text.encode()).decode()


def _mask(client_id: str) -> str:
    return (client_id[:4] + "…") if len(client_id) > 4 else "…"
