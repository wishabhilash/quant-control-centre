"""Fyers adapter: the documented headless login (five steps).

(1) send a login OTP to the Fyers id → ``request_key``; (2) verify the TOTP;
(3) verify the PIN → a short-lived pin access token; (4) request an auth code
for the registered app — Fyers answers **308** with a URL whose query carries
``auth_code``; (5) exchange the auth code at ``/api/v3/validate-authcode``
with ``appIdHash = sha256(client_id:secret_key)`` for the access token.

Retryable failures are the OTP/TOTP steps (a fresh 30-second TOTP window can
cure them); a wrong PIN or an app-level refusal is a hard failure. The
redirect URI is fixed: the headless login uses the app's registered redirect
and the auth code is read from the 308 response, never followed.
"""
from __future__ import annotations

import hashlib
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from app.brokers import base
from app.brokers.base import (
    BrokerAdapter,
    BrokerError,
    _mask,
    _now_iso,
)
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.broker import BrokerToken, FyersEnableRequest, FyersSecret

log = get_logger("services.broker.fyers")

FYERS_SEND_LOGIN_OTP_URL = "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2"
FYERS_VERIFY_OTP_URL = "https://api-t2.fyers.in/vagator/v2/verify_otp"
FYERS_VERIFY_PIN_URL = "https://api-t2.fyers.in/vagator/v2/verify_pin_v2"
FYERS_TOKEN_URL = "https://api-t1.fyers.in/api/v3/token"
FYERS_VALIDATE_AUTHCODE_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"

FYERS_REDIRECT_URI = "http://127.0.0.1:8080"


class FyersRetryable(Exception):
    """An OTP/TOTP-step failure a fresh TOTP window can cure."""


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


class FyersAdapter(BrokerAdapter):
    name = "fyers"
    enable_model = FyersEnableRequest
    secret_model = FyersSecret

    def normalize(self, req: Any) -> dict:
        creds = self.secret_model(
            username=(req.username or "").strip(),
            client_id=(req.client_id or "").strip(),
            secret_key=(req.secret_key or "").strip(),
            totp_key=(req.totp_key or "").strip().replace(" ", ""),
            pin=(req.pin or "").strip(),
        )
        missing = [f for f, v in (("Fyers username", creds.username),
                                  ("client id", creds.client_id),
                                  ("secret key", creds.secret_key),
                                  ("TOTP key", creds.totp_key),
                                  ("PIN", creds.pin))
                   if not v]
        if missing:
            raise BrokerError(f"{', '.join(missing)} required for {self.name}")
        return creds.model_dump()

    def totp_key(self, creds: dict) -> str:
        return creds["totp_key"]

    def generate_token(self, creds: dict, settings: Settings) -> BrokerToken:
        """Generate a Fyers access token (the five-step headless login).

        Retrying mirrors the Dhan protocol: the OTP steps can legitimately
        fail while the TOTP window rolls over, so those get a fresh cycle;
        a wrong PIN or an app refusal is a hard failure (retrying won't help).
        """
        import pyotp

        creds = FyersSecret.model_validate(creds)
        max_attempts = max(1, settings.broker_connect_attempts)
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
                    base._sleep(base._seconds_to_next_totp_cycle() + 1)
                continue
            except BrokerError:
                raise  # hard failure — retrying won't help
            except Exception as exc:  # noqa: BLE001 — logged WITHOUT details
                last_error = f"request error ({type(exc).__name__})"
                log.warning("attempt %s: request error (%s)", attempt,
                            type(exc).__name__)
                base._sleep(2)
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
                base._sleep(2)
                continue

            access_token = exchange.get("access_token") or ""
            if not access_token:
                raise BrokerError("Fyers's response carried no access token")
            log.info("fyers access token generated (client %s)",
                     _mask(creds.client_id))
            return BrokerToken(access_token=access_token,
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
