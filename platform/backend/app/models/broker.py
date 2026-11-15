"""Broker connection payloads (Dhan).

A user connects a broker once; the platform then keeps its access token fresh
(the Dhan token expires daily, so it is regenerated at 08:00 IST).

**Secrets never touch S3 or a strategy document** (golden rule 7): the TOTP
secret, client id and PIN are stored in AWS Secrets Manager (LocalStack in
development); only non-secret *metadata* — status, timestamps, last error —
becomes an S3 document at ``brokers/{user_id}/{broker}.json``.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from app.models.common import ApiModel

BrokerName = Literal["dhan"]
BrokerStatus = Literal["connecting", "connected", "failed"]


class DhanEnableRequest(ApiModel):
    """What the user types into the *Enable* form. Never echoed back."""

    totp_secret: str
    client_id: str
    pin: str


class DhanSecret(ApiModel):
    """The Secrets Manager payload for a Dhan connection."""

    client_id: str
    pin: str
    totp_secret: str


class DhanToken(ApiModel):
    """The Secrets Manager payload for a Dhan access token."""

    access_token: str
    generated_at: str
    # The raw Dhan response (its shape is Dhan's own; stored verbatim so a
    # strategy can read expiry etc. without the platform knowing every field).
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
