"""Broker connection payloads (Dhan, Fyers, …).

A user connects a broker once; the platform then keeps its access token fresh
(broker tokens expire daily, so they are regenerated at 08:00 IST). Each
broker's *protocol* lives in an adapter under ``app/brokers/``; this module
holds the DTOs — the enable-request forms, the SSM credential payloads, the
generic access-token payload, and the stored non-secret metadata. Adding a
broker: new adapter + one ``ADAPTERS`` entry + one ``BrokerName`` literal
(guarded by a test) + its DTOs here.

**Secrets never touch S3 or a strategy document** (golden rule 7): the TOTP
key, client id, PIN and app secret are stored in AWS Parameter Store (SSM)
(LocalStack in development); only non-secret *metadata* — status, timestamps,
last error — becomes an S3 document at ``brokers/{user_id}/{broker}.json``.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from app.models.common import ApiModel

BrokerName = Literal["dhan", "fyers"]
BrokerStatus = Literal["connecting", "connected", "failed"]


class DhanEnableRequest(ApiModel):
    """What the user types into the Dhan *Enable* form. Never echoed back."""

    totp_secret: str
    client_id: str
    pin: str


class FyersEnableRequest(ApiModel):
    """What the user types into the Fyers *Enable* form. Never echoed back.

    ``client_id`` is the Fyers app id (e.g. ``AB12CD-100``); ``username`` is
    the Fyers login id; ``secret_key`` is the app's secret (needed to exchange
    the auth code for a token); ``totp_key`` is the base32 TOTP secret from
    the authenticator setup.
    """

    username: str
    client_id: str
    secret_key: str
    totp_key: str
    pin: str


class DhanSecret(ApiModel):
    """The Parameter Store (SSM) payload for a Dhan connection."""

    client_id: str
    pin: str
    totp_secret: str


class FyersSecret(ApiModel):
    """The Parameter Store (SSM) payload for a Fyers connection."""

    username: str
    client_id: str
    secret_key: str
    totp_key: str
    pin: str


class BrokerToken(ApiModel):
    """The Parameter Store (SSM) payload for any broker's access token.

    One shape for every broker: ``access_token`` + ``generated_at`` + the raw
    broker response (its shape is the broker's own; stored verbatim so a
    strategy can read expiry etc. without the platform knowing every field).
    """

    access_token: str
    generated_at: str
    response: dict = {}


class BrokerDocument(ApiModel):
    """The stored (non-secret) connection record."""

    user_id: str
    broker: BrokerName
    status: BrokerStatus
    enabled_at: Optional[str] = None
    last_token_at: Optional[str] = None
    last_error: Optional[str] = None


class BrokerInfo(ApiModel):
    """What the API tells the UI about a connection (never a credential)."""

    broker: BrokerName
    status: BrokerStatus
    enabled_at: Optional[str] = None
    last_token_at: Optional[str] = None
    last_error: Optional[str] = None
    # Next scheduled token refresh (from the in-process scheduler), if any.
    next_refresh_at: Optional[str] = None


class BrokerListResponse(ApiModel):
    brokers: List[BrokerInfo]
