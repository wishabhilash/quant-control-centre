"""Dhan adapter: the access-token protocol.

``POST https://auth.dhan.co/app/generateAccessToken
?dhanClientId=..&pin=..&totp=..`` with retries while Dhan answers
``{"status": "error", "message": "Invalid TOTP"}`` — each retry waits for the
next 30-second TOTP cycle, since the token that was generated may have expired
between building the request and Dhan reading it. Any other refusal is a hard
failure (retrying won't help).
"""
from __future__ import annotations

from typing import Any, Tuple

from app.brokers import base
from app.brokers.base import (
    BrokerAdapter,
    BrokerError,
    _mask,
    _now_iso,
)
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.broker import BrokerToken, DhanEnableRequest, DhanSecret

log = get_logger("services.broker.dhan")

DHAN_AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"


def _dhan_request(url: str, timeout: int = 10) -> Tuple[int, dict]:
    """The single HTTP touchpoint with Dhan (isolated for tests)."""
    import requests

    response = requests.post(url, timeout=timeout)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body


class DhanAdapter(BrokerAdapter):
    name = "dhan"
    enable_model = DhanEnableRequest
    secret_model = DhanSecret

    def normalize(self, req: Any) -> dict:
        creds = self.secret_model(
            client_id=(req.client_id or "").strip(),
            pin=(req.pin or "").strip(),
            totp_secret=(req.totp_secret or "").strip().replace(" ", ""),
        )
        missing = [f for f, v in (("client id", creds.client_id),
                                  ("PIN", creds.pin),
                                  ("TOTP secret", creds.totp_secret))
                   if not v]
        if missing:
            raise BrokerError(f"{', '.join(missing)} required for {self.name}")
        return creds.model_dump()

    def totp_key(self, creds: dict) -> str:
        return creds["totp_secret"]

    def generate_token(self, creds: dict, settings: Settings) -> BrokerToken:
        """Generate an access token, retrying while Dhan says ``Invalid TOTP``.

        A TOTP code is valid for 30 seconds; if the request is built just
        before the window rolls over, Dhan may read the *next* code. Waiting
        for the fresh cycle and retrying is the documented cure.
        """
        creds = DhanSecret.model_validate(creds)
        max_attempts = max(1, settings.broker_connect_attempts)
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
                base._sleep(2)
                continue

            if (res_json.get("status") == "error"
                    and res_json.get("message") == "Invalid TOTP"):
                last_error = "invalid TOTP"
                log.warning("attempt %s: Dhan rejected the TOTP — waiting for "
                            "the next cycle", attempt)
                if attempt < max_attempts:
                    base._sleep(base._seconds_to_next_totp_cycle() + 1)
                continue

            if status_code == 200 and res_json.get("status") != "error":
                access_token = (res_json.get("accessToken")
                                or res_json.get("access_token") or "")
                if not access_token:
                    raise BrokerError("Dhan's response carried no access token")
                log.info("dhan access token generated (client %s)",
                         _mask(creds.client_id))
                return BrokerToken(access_token=access_token,
                                   generated_at=_now_iso(), response=res_json)

            # Any other answer is a hard failure — retrying won't help.
            message = res_json.get("message") or f"HTTP {status_code}"
            raise BrokerError(f"Dhan refused the request: {message}")
        raise BrokerError(
            f"could not generate the Dhan token after {max_attempts} attempts "
            f"({last_error or 'unknown error'})")
