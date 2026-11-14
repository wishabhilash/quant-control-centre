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
