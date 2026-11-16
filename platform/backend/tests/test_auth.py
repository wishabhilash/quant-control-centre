"""User authentication and per-user ownership.

The mobile number is the username; passwords are stored only as salted PBKDF2
hashes. Every strategy belongs to the user who created it: the list is filtered
by the caller and the single-strategy routes enforce ownership — the operator's
token is NOT a bypass, and the operator can also block (suspend) an account,
which kills its sign-in and every session token at once.
"""
import pytest

from app.core.security import (
    decode_token,
    hash_password,
    issue_token,
    verify_password,
)
from app.models.user import SigninRequest, SignupRequest
from app.models.strategy import StrategyCreate
from app.services.user_service import AuthError, UserService


# --- passwords --------------------------------------------------------------
def test_password_is_hashed_and_verified():
    digest, salt = hash_password("s3cret-pass")
    assert digest != "s3cret-pass"
    assert len(salt) == 32  # 16 random bytes, hex
    assert verify_password("s3cret-pass", digest, salt)
    assert not verify_password("wrong-pass", digest, salt)


def test_same_password_gets_different_hashes():
    d1, s1 = hash_password("same")
    d2, s2 = hash_password("same")
    assert (d1, s1) != (d2, s2)  # per-user salt


# --- tokens -----------------------------------------------------------------
def test_token_roundtrip(settings):
    token = issue_token("USR-1", "9999999999", settings)
    payload = decode_token(token, settings)
    assert payload["sub"] == "USR-1"
    assert payload["mob"] == "9999999999"


def test_tampered_token_is_rejected(settings):
    token = issue_token("USR-1", "9999999999", settings)
    body, sig = token.split(".")
    assert decode_token(f"{body}.{'x' * len(sig)}", settings) is None
    assert decode_token("garbage", settings) is None


def test_expired_token_is_rejected(settings):
    token = issue_token("USR-1", "9999999999", settings, ttl_seconds=-10)
    assert decode_token(token, settings) is None


# --- signup / signin --------------------------------------------------------
def test_signup_then_signin(user_service):
    res = user_service.signup(SignupRequest(full_name="Asha Rao",
                                           mobile="9876543210",
                                           password="hunter22"))
    assert res.token
    assert res.user.mobile == "9876543210"
    assert res.user.full_name == "Asha Rao"
    # The password is never echoed back.
    assert "hunter22" not in res.model_dump_json()

    again = user_service.signin(SigninRequest(mobile="9876543210", password="hunter22"))
    assert again.user.user_id == res.user.user_id


def test_signup_rejects_duplicate_mobile(user_service):
    user_service.signup(SignupRequest(full_name="A", mobile="9876543210", password="pw12345"))
    with pytest.raises(AuthError):
        user_service.signup(SignupRequest(full_name="B", mobile="9876543210", password="pw12345"))


def test_signin_with_wrong_password_fails(user_service):
    user_service.signup(SignupRequest(full_name="A", mobile="9876543210", password="pw12345"))
    with pytest.raises(AuthError):
        user_service.signin(SigninRequest(mobile="9876543210", password="nope"))


def test_signin_with_unknown_mobile_fails(user_service):
    with pytest.raises(AuthError):
        user_service.signin(SigninRequest(mobile="0000000000", password="pw12345"))


# --- ownership --------------------------------------------------------------
def test_strategy_is_owned_by_its_creator(strategy_service, user_service):
    owner = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                               password="pw12345")).user.user_id
    doc = strategy_service.create(StrategyCreate(name="Mine", files={"main.py": "print(1)"}),
                                   owner_id=owner)
    assert doc.owner_id == owner
    assert strategy_service.get(doc.strategy_id).owner_id == owner


def test_list_is_filtered_by_owner(strategy_service, user_service):
    a = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                          password="pw12345")).user.user_id
    b = user_service.signup(SignupRequest(full_name="B", mobile="9876543211",
                                          password="pw12345")).user.user_id
    doc_a = strategy_service.create(StrategyCreate(name="A-strat",
                                                   files={"main.py": "print(1)"}), owner_id=a)
    strategy_service.create(StrategyCreate(name="B-strat", files={"main.py": "print(1)"}),
                            owner_id=b)

    class _P:
        def __init__(self, user_id):
            self.user_id = user_id
            self.is_admin = False

    listed_a = strategy_service.list(_P(a))
    assert [s.name for s in listed_a.strategies] == ["A-strat"]
    listed_b = strategy_service.list(_P(b))
    assert [s.name for s in listed_b.strategies] == ["B-strat"]


def test_user_cannot_read_another_users_strategy(strategy_service, user_service):
    from app.services.strategy_service import StrategyServiceError, ensure_access

    a = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                          password="pw12345")).user.user_id
    b = user_service.signup(SignupRequest(full_name="B", mobile="9876543211",
                                          password="pw12345")).user.user_id
    doc = strategy_service.create(StrategyCreate(name="A-strat",
                                                 files={"main.py": "print(1)"}), owner_id=a)

    class _P:
        def __init__(self, user_id, is_admin=False):
            self.user_id = user_id
            self.is_admin = is_admin

    # The owner may read it.
    strategy_service.get_for_user(doc.strategy_id, _P(a))
    # Another user may not.
    with pytest.raises(StrategyServiceError):
        strategy_service.get_for_user(doc.strategy_id, _P(b))
    # The operator may NOT either: administrative access to user data is an
    # explicit action (assign/claim), not an ambient capability.
    with pytest.raises(StrategyServiceError):
        strategy_service.get_for_user(doc.strategy_id, _P("admin", is_admin=True))


def test_operator_list_is_empty_and_unassigned_are_listed(strategy_service, user_service):
    """The operator sees no user strategies; unowned ones are listed for claiming."""
    owner = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                              password="pw12345")).user.user_id
    owned = strategy_service.create(StrategyCreate(name="Owned",
                                                   files={"main.py": "print(1)"}), owner_id=owner)
    legacy = strategy_service.create(StrategyCreate(name="Legacy",
                                                    files={"main.py": "print(1)"}))
    assert legacy.owner_id is None

    class _P:
        def __init__(self, user_id, is_admin=False):
            self.user_id = user_id
            self.is_admin = is_admin

    # The operator's own list is empty (it owns nothing).
    assert strategy_service.list(_P("admin", is_admin=True)).count == 0
    # The owner sees exactly their own.
    listed = strategy_service.list(_P(owner))
    assert [s.name for s in listed.strategies] == ["Owned"]
    # The legacy (unowned) documents are visible for claiming.
    assert [d.strategy_id for d in strategy_service.list_unassigned()] == [legacy.strategy_id]
    # …and an administrator can assign one to a user.
    strategy_service.assign_owner(legacy.strategy_id, owner)
    assert strategy_service.get(legacy.strategy_id).owner_id == owner
    assert strategy_service.list_unassigned() == []


# --- stale tokens (the silent-admin regression) ------------------------------
def test_stale_token_is_rejected_even_when_auth_is_disabled(settings, user_service,
                                                            monkeypatch):
    """A presented token that no longer verifies must raise 401 — never
    silently become the operator.

    Regression: with no ``AUTH_SIGNING_SECRET`` (dev default), tokens are
    signed with a random per-process secret; after a backend restart a stale
    browser token stopped verifying but was silently treated as the admin
    principal — the dashboard kept showing the user's name while the backend
    acted under ``admin`` (a broker connection was stored under the wrong
    account this way).
    """
    import asyncio

    from fastapi.security import HTTPAuthorizationCredentials

    from app.core import security
    from app.core.security import SecurityError, get_current_user, issue_token
    import app.api.deps as deps

    # The backend restarted: the signing secret changed (dev default was a
    # random per-process key), so the browser's stored token no longer
    # verifies. Authentication itself is still disabled (dev).
    token = issue_token("USR-X", "9876543210", settings)
    restarted = security.Settings(environment="test",
                                  platform_root=settings.platform_root,
                                  scheduler_dry_run=True, auth_enabled=False,
                                  auth_signing_secret="a-different-dev-secret")
    monkeypatch.setattr(deps, "get_user_store", lambda: user_service.store)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with pytest.raises(SecurityError) as exc:
        asyncio.run(get_current_user(creds, restarted))
    assert exc.value.status_code == 401
    assert "sign in" in exc.value.detail


def test_valid_token_still_honoured_when_auth_is_disabled(settings, user_service,
                                                          monkeypatch):
    """The positive control: a token that *does* verify still authenticates
    its user locally (auth disabled does not force operator mode)."""
    import asyncio

    from fastapi.security import HTTPAuthorizationCredentials

    from app.core.security import get_current_user
    import app.api.deps as deps

    user = user_service.signup(SignupRequest(full_name="Asha Rao",
                                             mobile="9876543210",
                                             password="pw12345"))
    res = user_service.signin(SigninRequest(mobile="9876543210", password="pw12345"))
    monkeypatch.setattr(deps, "get_user_store", lambda: user_service.store)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=res.token)

    principal = asyncio.run(get_current_user(creds, settings))
    assert principal.user_id == user.user.user_id
    assert not principal.is_admin


def test_no_token_and_auth_disabled_is_the_operator(settings):
    """Without any presented token, auth-disabled requests stay operator
    (break-glass) — unchanged behaviour for the local developer."""
    import asyncio

    from app.core.security import ADMIN_PRINCIPAL, get_current_user

    assert asyncio.run(get_current_user(None, settings)) is ADMIN_PRINCIPAL


# --- brokers belong to a user, not to the operator ---------------------------
def test_operator_principal_is_refused_on_broker_routes():
    """The operator principal has no user account to attach a connection to;
    letting it enable a broker would store credentials under ``admin`` where
    no strategy (every strategy is user-owned) would ever see the token."""
    from fastapi import HTTPException

    from app.api.brokers import _user_principal
    from app.core.security import ADMIN_PRINCIPAL

    with pytest.raises(HTTPException) as exc:
        _user_principal(ADMIN_PRINCIPAL)
    assert exc.value.status_code == 400
    assert "sign in as the user" in exc.value.detail

    class _P:
        user_id = "USR-1"
        is_admin = False

    assert _user_principal(_P()) == "USR-1"


def test_broker_enable_never_reaches_the_service_for_the_operator(monkeypatch):
    """Route-level: for the operator principal the enable handler returns 400
    without touching the broker service."""
    import asyncio

    from fastapi import HTTPException

    from app.api.brokers import enable_broker
    from app.core.security import ADMIN_PRINCIPAL

    class _Boom:
        def enable(self, *_a, **_k):  # pragma: no cover — must never run
            raise AssertionError("broker service must not be called")

    body = {"totp_secret": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
            "client_id": "12345678", "pin": "112233"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(enable_broker("dhan", body, ADMIN_PRINCIPAL, _Boom()))
    assert exc.value.status_code == 400
    assert "sign in as the user" in exc.value.detail


# --- account blocking -------------------------------------------------------
def test_block_prevents_signin(user_service):
    """A blocked account cannot sign in, even with the right password."""
    user = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                             password="pw12345")).user
    # Before blocking, sign-in works.
    user_service.signin(SigninRequest(mobile="9876543210", password="pw12345"))

    user_service.block(user.user_id)
    with pytest.raises(AuthError):
        user_service.signin(SigninRequest(mobile="9876543210", password="pw12345"))
    # The account document carries the suspension.
    assert user_service.get_by_id(user.user_id).status == "blocked"

    # Unblocking restores sign-in.
    user_service.unblock(user.user_id)
    user_service.signin(SigninRequest(mobile="9876543210", password="pw12345"))
    assert user_service.get_by_id(user.user_id).status == "active"


def test_block_invalidates_existing_token(user_service, settings):
    """Blocking kills every session token at once: the token check re-reads the
    account document (S3 is the source of truth)."""
    from app.core.security import _principal_from_token

    res = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                            password="pw12345"))
    # The fresh token verifies.
    assert _principal_from_token(res.token, settings, user_service.store) is not None

    user_service.block(res.user.user_id)
    # The very same token now fails — no need to wait for it to expire.
    assert _principal_from_token(res.token, settings, user_service.store) is None

    user_service.unblock(res.user.user_id)
    assert _principal_from_token(res.token, settings, user_service.store) is not None


def test_admin_list_reflects_blocked_status(strategy_service, user_service):
    """The operator's user list must show the live status — a block has to be
    visible. (Regression: the list once omitted the status field and every
    account rendered 'active' even after being blocked.)"""
    from app.api.admin import list_users

    res = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                            password="pw12345"))
    listing = {u.mobile: u for u in list_users(user_service, strategy_service)}
    assert listing["9876543210"].status == "active"

    user_service.block(res.user.user_id)
    listing = {u.mobile: u for u in list_users(user_service, strategy_service)}
    assert listing["9876543210"].status == "blocked"
    assert listing["9876543210"].blocked_by == "operator"

    user_service.unblock(res.user.user_id)
    listing = {u.mobile: u for u in list_users(user_service, strategy_service)}
    assert listing["9876543210"].status == "active"


# --- password change / reset ------------------------------------------------
def test_token_carries_issued_at(settings):
    """Every token now carries ``iat`` so a later change can retire it."""
    from app.core.security import decode_token, issue_token

    payload = decode_token(issue_token("USR-1", "9999999999", settings), settings)
    assert "iat" in payload and payload["iat"] > 0


def test_password_change_retires_tokens_issued_before_it(settings, user_service):
    """The invalidation guard: a token whose ``iat`` predates
    ``password_changed_at`` no longer verifies; one issued at/after it does.

    This is the mechanism that signs out every *other* session when a password
    changes — the same instant, S3-backed check as blocking. (Second-granularity
    timestamps mean a token issued in the *same* second survives; that is the
    documented, negligible edge.)
    """
    import datetime as _dt

    from app.core.security import _principal_from_token, decode_token, issue_token

    res = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                            password="pw12345"))
    old = issue_token(res.user.user_id, res.user.mobile, settings, ttl_seconds=3600)
    iat = decode_token(old, settings)["iat"]
    record = user_service.get_by_id(res.user.user_id)

    # A change stamped strictly after the token → the token is retired.
    record.password_changed_at = (_dt.datetime.fromtimestamp(iat + 100)
                                 .astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat())
    user_service.store.update(record)
    assert _principal_from_token(old, settings, user_service.store) is None

    # A change stamped strictly before the token → the token still verifies.
    record.password_changed_at = (_dt.datetime.fromtimestamp(iat - 100)
                                 .astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat())
    user_service.store.update(record)
    assert _principal_from_token(old, settings, user_service.store) is not None


def test_change_password_requires_the_current(user_service):
    res = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                            password="old-pass"))
    with pytest.raises(AuthError):
        user_service.change_password(res.user.user_id, "wrong", "new-pass")


def test_change_password_swaps_the_stored_hash_and_returns_a_fresh_token(user_service):
    res = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                            password="old-pass"))
    new = user_service.change_password(res.user.user_id, "old-pass", "new-pass")
    # A fresh token is handed back so the caller's own session survives…
    assert new.token
    # …the new password signs in and the old one no longer does.
    user_service.signin(SigninRequest(mobile="9876543210", password="new-pass"))
    with pytest.raises(AuthError):
        user_service.signin(SigninRequest(mobile="9876543210", password="old-pass"))


def test_change_password_rejects_an_empty_new_password(user_service):
    res = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                            password="old-pass"))
    with pytest.raises(AuthError):
        user_service.change_password(res.user.user_id, "old-pass", "")


def test_operator_reset_password_swaps_hash_and_returns_it_once(user_service, strategy_service,
                                                                monkeypatch):
    """The operator sets a new password without knowing the old one; the new
    password is returned **once** and the old one stops working."""
    import app.api.deps as deps

    monkeypatch.setattr(deps, "get_strategy_service", lambda: strategy_service)
    res = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                            password="old-pass"))
    out = user_service.reset_password(res.user.user_id, "operator-chosen", actor="operator")
    # The new password is disclosed exactly once, in the response.
    assert out.new_password == "operator-chosen"
    assert out.user.user_id == res.user.user_id
    # …and the account document now carries the new hash (the old one is gone).
    reloaded = user_service.get_by_id(res.user.user_id)
    assert reloaded.password_changed_at is not None
    user_service.signin(SigninRequest(mobile="9876543210", password="operator-chosen"))
    with pytest.raises(AuthError):
        user_service.signin(SigninRequest(mobile="9876543210", password="old-pass"))


def test_account_info_returns_only_own_fields(settings, user_service, strategy_service,
                                              monkeypatch):
    """Account Settings shows the user's own info — never the hash."""
    import app.api.deps as deps

    monkeypatch.setattr(deps, "get_strategy_service", lambda: strategy_service)
    res = user_service.signup(SignupRequest(full_name="Asha Rao", mobile="9876543210",
                                            password="pw12345"))
    info = user_service.account_info(res.user.user_id)
    assert info.user_id == res.user.user_id
    assert info.full_name == "Asha Rao"
    assert info.mobile == "9876543210"
    assert info.strategy_count == 0
    # The hash never leaves the service.
    assert "password_hash" not in info.model_dump()


def test_reset_password_endpoint_is_operator_only(settings, user_service, monkeypatch):
    """A user token must NOT be able to reset a password — only the operator.

    The admin router guards every route with ``require_admin``; a user token is
    refused with 403.
    """
    import asyncio

    from fastapi.security import HTTPAuthorizationCredentials

    from app.core.security import SecurityError, issue_token, require_admin

    res = user_service.signup(SignupRequest(full_name="A", mobile="9876543210",
                                            password="pw12345"))
    user_token = issue_token(res.user.user_id, res.user.mobile, settings)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "dev_api_token", "the-operator-token")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=user_token)

    with pytest.raises(SecurityError) as exc:
        asyncio.run(require_admin(creds, settings))
    assert exc.value.status_code == 403
