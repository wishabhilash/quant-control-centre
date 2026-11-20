"""Copy trading: the compliant "run the algo for the follower" model.

On FOLLOW the platform creates a NEW strategy owned by the follower — a
deep copy of the leader's project, disabled, with the leader's schedules as
DISABLED drafts and copied_from attribution. The leader's source code is
never shown to the follower (code routes refuse). No signal mirroring
anywhere — the follower's instance runs on their own broker with their own
capital.
"""
from typing import Any, Dict

import json

import pytest

from app.models.strategy import StrategyCreate
from app.services.copy_service import CopyError, CopyService
from app.services.user_service import UserService, SignupRequest


def _signup(user_service: UserService, full_name: str, mobile: str) -> str:
    return user_service.signup(SignupRequest(full_name=full_name,
                                             mobile=mobile,
                                             password="pw12345")).user.user_id


def _publish(service: CopyService, strategy_service, leader_id: str,
             name="BTST Stock Futs") -> str:
    doc = strategy_service.create(StrategyCreate(
        name=name,
        description="the daily BTST",
        enabled=True,
        parameters={"MAX_LOTS_CAP": 10},
        files={"main.py": "print('btst')\n",
               "requirements.txt": "pandas\n"},
    ), owner_id=leader_id)
    service.set_copy_trading(doc.strategy_id, True)
    return doc.strategy_id


@pytest.fixture
def copy_svc(strategy_service, settings, fake_s3, user_service):
    return CopyService(settings, strategy_service, user_service,
                       store=strategy_service.store)


def _user_service(strategy_service, settings, fake_s3):
    from app.services.user_service import UserService
    from app.storage.user_store import UserStore

    return UserService(settings, UserStore(fake_s3))


@pytest.fixture
def user_service(strategy_service, settings, fake_s3):
    return _user_service(strategy_service, settings, fake_s3)


# --- leader side --------------------------------------------------------------
def test_copy_trading_toggle_is_owner_scoped(copy_svc, strategy_service,
                                             user_service):
    leader = _signup(user_service, "Leader", "9000000001")
    sid = _publish(copy_svc, strategy_service, leader)
    doc = strategy_service.get(sid)
    assert doc.copy_trading is True
    copy_svc.set_copy_trading(sid, False)
    assert strategy_service.get(sid).copy_trading is False


def test_a_copied_strategy_cannot_be_offered(copy_svc, strategy_service,
                                             user_service):
    leader = _signup(user_service, "Leader", "9000000002")
    follower = _signup(user_service, "Follower", "9000000003")
    sid = _publish(copy_svc, strategy_service, leader)
    copy_svc.follow(sid, follower)
    copied = copy_svc.mine(follower)[0]["strategy_id"]
    with pytest.raises(CopyError):
        copy_svc.set_copy_trading(copied, True)  # no chains


# --- universe -----------------------------------------------------------------
def test_universe_lists_published_strategies_across_accounts(
        copy_svc, strategy_service, user_service):
    leader = _signup(user_service, "Leader", "9000000004")
    follower = _signup(user_service, "Follower", "9000000005")
    other_leader = _signup(user_service, "Other", "9000000006")
    sid = _publish(copy_svc, strategy_service, leader)
    private = strategy_service.create(StrategyCreate(
        name="Private", files={"main.py": "x=1"}), owner_id=other_leader)

    universe = copy_svc.universe(follower)

    ids = [s["strategy_id"] for s in universe["universe"]]
    assert sid in ids and private.strategy_id not in ids
    entry = next(s for s in universe["universe"] if s["strategy_id"] == sid)
    assert entry["owner_name"] == "Leader"
    assert entry["followers"] == 0
    assert "main.py" not in str(entry)  # no source exposure


def test_universe_marks_the_callers_own_strategy_not_followable(
        copy_svc, strategy_service, user_service):
    """The creator SEES their own listing (it is their storefront — publish
    → it appears, with the live follower count), but it is marked ``is_mine``
    so the UI never offers a follow button on it."""
    leader = _signup(user_service, "Leader", "9000000007")
    sid = _publish(copy_svc, strategy_service, leader)
    universe = copy_svc.universe(leader)
    entry = next(s for s in universe["universe"] if s["strategy_id"] == sid)
    assert entry["is_mine"] is True
    assert entry["owner_name"] == "Leader"
    # A follower sees the same listing without the marker (they can follow).
    follower = _signup(user_service, "Follower", "9000000099")
    other_view = next(s for s in copy_svc.universe(follower)["universe"]
                      if s["strategy_id"] == sid)
    assert other_view["is_mine"] is False


def test_follower_count_reflects_copies(copy_svc, strategy_service,
                                        user_service):
    leader = _signup(user_service, "Leader", "9000000008")
    sid = _publish(copy_svc, strategy_service, leader)
    for mobile in ("9000000009", "9000000010"):
        follower = _signup(user_service, "F", mobile)
        copy_svc.follow(sid, follower)
    entry = next(s for s in copy_svc.universe("9000000009")["universe"]
                 if s["strategy_id"] == sid)
    assert entry["followers"] == 2


# --- follow -------------------------------------------------------------------
def test_follow_creates_the_followers_own_disabled_copy(
        copy_svc, strategy_service, user_service):
    leader = _signup(user_service, "Leader", "9000000011")
    follower = _signup(user_service, "Follower", "9000000012")
    sid = _publish(copy_svc, strategy_service, leader)
    doc = strategy_service.get(sid)
    doc.schedules.append(__import__("app.models.strategy", fromlist=["Schedule"]
                                    ).Schedule(
        schedule_id="SCH-001", name="Morning exit",
        cron_expression="20 9 * * 1-5", enabled=True, command="--slot 0920"))
    strategy_service.store.update_document(doc)

    described = copy_svc.follow(sid, follower)
    new_id = described["strategy_id"]

    copied = strategy_service.get(new_id)
    assert copied.owner_id == follower
    assert copied.enabled is False
    assert copied.copied_from is not None
    assert copied.copied_from.strategy_id == sid
    assert copied.copied_from.owner_id == leader
    # The project files are a real copy (the follower runs the same algo)…
    assert "main.py" in copied.code.files
    assert "requirements.txt" in copied.code.files
    # …parameters carried over…
    assert copied.configuration.parameters == {"MAX_LOTS_CAP": 10}
    # …and schedules are DISABLED drafts with fresh ids.
    assert len(copied.schedules) == 1
    assert copied.schedules[0].enabled is False
    assert copied.schedules[0].cron_expression == "20 9 * * 1-5"
    assert copied.schedules[0].schedule_id == "SCH-001"
    # The leader's doc is untouched (their schedules stay enabled).
    assert strategy_service.get(sid).schedules[0].enabled is True


def test_follow_rejects_unpublished_and_own(copy_svc, strategy_service,
                                            user_service):
    leader = _signup(user_service, "Leader", "9000000013")
    follower = _signup(user_service, "Follower", "9000000014")
    private = strategy_service.create(StrategyCreate(
        name="Private", files={"main.py": "x=1"}), owner_id=leader)
    with pytest.raises(CopyError):
        copy_svc.follow(private.strategy_id, follower)  # not published
    published = _publish(copy_svc, strategy_service, leader)
    with pytest.raises(CopyError):
        copy_svc.follow(published, leader)  # your own


def test_marketplace_index_document_is_maintained(copy_svc, strategy_service,
                                                  user_service, fake_s3):
    """S3 is a DOCUMENT store: the Marketplace reads ONE index document
    (strategies/index.json) instead of scanning N strategy documents. The
    index is a rebuildable projection — publishing and following update it
    inline, and deleting a strategy removes its offering AND its copies."""
    leader = _signup(user_service, "Leader", "9000000030")
    follower = _signup(user_service, "Follower", "9000000031")
    sid = _publish(copy_svc, strategy_service, leader, name="BTST Stock Futs")

    index = json.loads(fake_s3.get_object("strategies/index.json"))
    assert any(e["strategy_id"] == sid for e in index["entries"])
    assert index["copies"] == []

    copy_svc.follow(sid, follower)
    index = json.loads(fake_s3.get_object("strategies/index.json"))
    assert any(e["strategy_id"] == sid for e in index["entries"])
    assert len(index["copies"]) == 1
    assert index["copies"][0]["owner_id"] == follower

    # The follower's Mine reads the index, not a full scan.
    assert len(copy_svc.mine(follower)) == 1


def test_marketplace_index_rebuilds_when_missing(copy_svc, strategy_service,
                                                 user_service, fake_s3):
    """The index is a PROJECTION: delete it and the next read rebuilds it
    from the strategy documents — no data can be lost by index corruption."""
    leader = _signup(user_service, "Leader", "9000000032")
    follower = _signup(user_service, "Follower", "9000000033")
    sid = _publish(copy_svc, strategy_service, leader)
    copy_svc.follow(sid, follower)

    fake_s3.delete_object("strategies/index.json")  # corrupt/lost
    universe = copy_svc.universe(follower)
    assert universe["total"] == 1
    entry = universe["universe"][0]
    assert entry["strategy_id"] == sid and entry["is_mine"] is False
    # …and the rebuilt index is persisted again for the next read.
    index = json.loads(fake_s3.get_object("strategies/index.json"))
    assert any(c["copy_id"] for c in index["copies"])
    leader = _signup(user_service, "Leader", "9000000015")
    follower = _signup(user_service, "Follower", "9000000016")
    sid = _publish(copy_svc, strategy_service, leader, name="BTST Stock Futs")
    copy_svc.follow(sid, follower)
    mine = copy_svc.mine(follower)
    assert len(mine) == 1
    assert mine[0]["name"] == "BTST Stock Futs (copy)"
    assert mine[0]["source_name"] == "BTST Stock Futs"
    assert mine[0]["enabled"] is False
    assert mine[0]["deployment_status"] == "not_deployed"
    # Another user's mine is empty.
    stranger = _signup(user_service, "S", "9000000017")
    assert copy_svc.mine(stranger) == []


def test_summary_carries_the_real_copy_trading_state(strategy_service, user_service,
                                                     settings, fake_s3):
    """The Strategies page toggle reads the list summary — it must carry the
    REAL copy-trading state (regression: to_summary defaulted copy_trading
    to False, so a published strategy displayed 'copy off' while it was live
    in the Universe)."""
    from app.services.copy_service import CopyService

    leader = user_service.signup(SignupRequest(
        full_name="L", mobile="9000000101", password="pw12345")).user.user_id
    doc = strategy_service.create(StrategyCreate(
        name="One", files={"main.py": "x=1"}), owner_id=leader)
    copy = CopyService(settings, strategy_service, user_service,
                       store=strategy_service.store)
    copy.set_copy_trading(doc.strategy_id, True)

    summary = strategy_service.to_summary(strategy_service.get(doc.strategy_id))
    assert summary.copy_trading is True
    assert summary.copied is False

    copy.set_copy_trading(doc.strategy_id, False)
    summary = strategy_service.to_summary(strategy_service.get(doc.strategy_id))
    assert summary.copy_trading is False


# --- the source stays hidden --------------------------------------------------
def test_copied_strategy_code_routes_refuse_the_follower(
        strategy_service, settings, fake_s3, user_service):
    """The follower controls runs, schedules and logs — but the leader's
    source code is never shown (consent + IP stay with the author)."""
    from fastapi import HTTPException

    from app.api import code as code_api
    from app.api.deps import get_strategy_service  # noqa: F401
    from app.core.security import Principal
    from app.services.copy_service import CopyService
    from app.storage.strategy_store import StrategyStore

    leader = _signup(user_service, "Leader", "9000000018")
    follower = _signup(user_service, "Follower", "9000000019")
    sid = _publish(CopyService(settings, strategy_service, user_service,
                               store=strategy_service.store),
                   strategy_service, leader)
    copy_svc = CopyService(settings, strategy_service, user_service,
                           store=strategy_service.store)
    copy_svc.follow(sid, follower)
    copied_id = copy_svc.mine(follower)[0]["strategy_id"]

    class _Principal:
        user_id = follower

    class _Strategies:
        def get_for_user(self, sid, principal):
            return strategy_service.get(sid)

        def get(self, sid):
            return strategy_service.get(sid)

    store = StrategyStore(fake_s3)
    doc = store.get_document(copied_id)
    assert doc.copied_from is not None

    with pytest.raises(HTTPException) as excinfo:
        code_api._ensure_source_viewable(doc)
    # 400, deliberately: 403 would fire the client's auth-required event
    # (which logs the follower OUT — a business denial must never kill
    # the session, the same convention as change-password's 400s).
    assert excinfo.value.status_code == 400

    with pytest.raises(HTTPException) as excinfo:
        code_api.get_code(copied_id, store=store, principal=_Principal(),
                          svc=_Strategies())
    assert "not viewable" in excinfo.value.detail
    assert excinfo.value.status_code == 400

    with pytest.raises(HTTPException):
        code_api.save_code(copied_id, update=None, principal=_Principal(),
                           svc=_Strategies(), strategies=_Strategies())


def test_universe_marks_strategies_the_viewer_already_follows(
        copy_svc, strategy_service, user_service):
    """Already-following is visible: the Universe marks ``is_followed`` so
    the UI shows 'following' instead of a second Follow button."""
    leader = _signup(user_service, "Leader", "9000000021")
    follower = _signup(user_service, "Follower", "9000000022")
    sid = _publish(copy_svc, strategy_service, leader)
    copy_svc.follow(sid, follower)

    entry = next(s for s in copy_svc.universe(follower)["universe"]
                 if s["strategy_id"] == sid)
    assert entry["is_followed"] is True
    # The leader's view of the same strategy: not followed by THEM.
    leader_view = next(s for s in copy_svc.universe(leader)["universe"]
                       if s["strategy_id"] == sid)
    assert leader_view["is_followed"] is False


def test_follow_twice_is_rejected(copy_svc, strategy_service, user_service):
    leader = _signup(user_service, "Leader", "9000000023")
    follower = _signup(user_service, "Follower", "9000000024")
    sid = _publish(copy_svc, strategy_service, leader)
    copy_svc.follow(sid, follower)
    with pytest.raises(CopyError) as excinfo:
        copy_svc.follow(sid, follower)
    assert "already follow" in str(excinfo.value)


def test_universe_search_and_pagination(copy_svc, strategy_service,
                                        user_service):
    """Server-side search + pagination: the Marketplace must stay usable at
    hundreds of published strategies (1000 demo strategies are loaded in the
    dev bucket)."""
    leader = _signup(user_service, "Author", "9000000025")
    for i in range(1, 26):  # 25 published strategies
        doc = strategy_service.create(StrategyCreate(
            name=f"{'Alpha' if i % 2 else 'Beta'} {i:03d}",
            description=f"demo {i}",
            enabled=True,
            parameters={},
            files={"main.py": "x=1"},
        ), owner_id=leader)
        copy_svc.set_copy_trading(doc.strategy_id, True)

    # pagination slices the sorted result and reports the totals
    page1 = copy_svc.universe(leader, page=1, page_size=10)
    assert page1["total"] == 25 and page1["pages"] == 3
    assert len(page1["universe"]) == 10
    page3 = copy_svc.universe(leader, page=3, page_size=10)
    assert len(page3["universe"]) == 5
    # no duplicates across pages
    ids = [s["strategy_id"] for s in page1["universe"] + page3["universe"]]
    assert len(ids) == len(set(ids))

    # search matches name (case-insensitive), then paginates the matches
    hit = copy_svc.universe(leader, query="alpha", page_size=10)
    assert hit["total"] == 13 and len(hit["universe"]) == 10
    assert all("alpha" in s["name"].lower() for s in hit["universe"])
    assert copy_svc.universe(leader, query="nomatch-xyz")["total"] == 0


# --- the Strategies page pagination -------------------------------------------
def test_strategies_list_paginates_most_recent_first(strategy_service,
                                                     user_service, settings,
                                                     fake_s3):
    """The Strategies page shows a page of the caller's own strategies (most
    recently edited first), with totals — the demo author owns 1000."""
    import time as t

    from app.core.security import Principal

    leader = _signup(user_service, "Author", "9000000101")
    principal = Principal(user_id=leader, mobile="9000000101",
                          full_name="Author")
    for i in range(1, 26):
        strategy_service.create(StrategyCreate(
            name=f"Strat {i:02d}", files={"main.py": "x=1"}), owner_id=leader)
        t.sleep(0.01)  # distinct updated_at stamps for deterministic order

    page1 = strategy_service.list(principal, page=1, page_size=10)
    assert page1.total == 25 and page1.pages == 3 and page1.count == 10
    # most recently edited first → the LAST created strategy leads page 1
    assert page1.strategies[0].name == "Strat 25"
    names_all = ([s.name for s in page1.strategies]
                 + [s.name for s in strategy_service.list(principal, page=2,
                                             page_size=10).strategies]
                 + [s.name for s in strategy_service.list(principal, page=3,
                                             page_size=10).strategies])
    assert len(names_all) == 25 and len(set(names_all)) == 25
    # another user's strategies never leak in
    assert all(n.startswith("Strat") for n in names_all)
    out_of_range = strategy_service.list(principal, page=9, page_size=10)
    assert out_of_range.strategies == [] and out_of_range.total == 25


def test_universe_discloses_whether_the_author_deploys(
        copy_svc, strategy_service, user_service):
    """Followers deserve to know if the author runs their own strategy:
    the entry carries author_deployed (a never-deployed publish is allowed —
    the author may write FOR others — but disclosed)."""
    leader = _signup(user_service, "Author", "9000000034")
    undeployed = strategy_service.create(StrategyCreate(
        name="Undeployed", files={"main.py": "x=1"}), owner_id=leader)
    copy_svc.set_copy_trading(undeployed.strategy_id, True)
    deployed = strategy_service.create(StrategyCreate(
        name="Deployed", files={"main.py": "x=1"}), owner_id=leader)
    # a real deploy stamps last_deployed_at AND source_hash (the deploy
    # service is the only writer of the snapshot pointer); deploy BEFORE
    # publishing — the publish then captures the disclosure
    doc = strategy_service.get(deployed.strategy_id)
    doc.deployment.last_deployed_at = "2026-11-19T09:30:00Z"
    strategy_service.store.update_document(doc)
    copy_svc.set_copy_trading(deployed.strategy_id, True)

    universe = copy_svc.universe("9000000034")
    by_id = {s["strategy_id"]: s for s in universe["universe"]}
    assert by_id[undeployed.strategy_id]["author_deployed"] is False
    assert by_id[deployed.strategy_id]["author_deployed"] is True


def test_a_deploy_refreshes_the_marketplace_disclosure(
        copy_svc, strategy_service, user_service, settings, monkeypatch):
    """The user-visible bug: publish BEFORE deploying → the index entry says
    'never deployed' even after a deploy. A successful deploy must refresh the
    entry inline (the hourly rebuild is only the backstop)."""
    from app.api import deploy as deploy_api
    from app.core.security import Principal
    from app.services.deploy_service import DeployService
    import shutil as _shutil
    import subprocess as _sp

    leader = _signup(user_service, "Author", "9000000036")
    doc = strategy_service.create(StrategyCreate(
        name="Late Deploy", files={"main.py": "print('x')\n"}), owner_id=leader)
    copy_svc.set_copy_trading(doc.strategy_id, True)
    before = copy_svc.universe(leader)["universe"][0]
    assert before["author_deployed"] is False

    # the deploy, offline (subprocess mocked — see test_deploy.py)
    calls: list = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "venv" in cmd:
            from pathlib import Path
            bin_dir = Path(cmd[cmd.index("venv") + 1]) / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "python").write_text("#!/bin/sh\n")
            (bin_dir / "pip").write_text("#!/bin/sh\n")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr("app.services.deploy_service.subprocess.run", fake_run)
    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/python3")
    deploy_svc = DeployService(settings, strategy_service)

    deploy_api.deploy(
        doc.strategy_id, wait_if_running=False,
        principal=Principal(leader, "9000000036", "Author"),
        svc=deploy_svc, strategies=strategy_service, copy=copy_svc)

    entry = copy_svc.universe(leader)["universe"][0]
    assert entry["author_deployed"] is True, (
        "a deploy must refresh the marketplace entry inline")


def test_a_follower_can_pull_the_leaders_update(
        copy_svc, strategy_service, user_service, settings, monkeypatch):
    """The frozen snapshot never propagates on its own; the FOLLOWER pulls.
    Leader edits after the follow → leader_updated on the copy's summary;
    pull_leader_update re-copies the code, clears the flag, and deploys."""
    from app.core.security import Principal
    from app.services.deploy_service import DeployService
    from app.models.strategy import CodeUpdate
    from app.services.code_service import CodeService
    import shutil as _shutil

    leader = _signup(user_service, "Author", "9000000037")
    follower = _signup(user_service, "Follower", "9000000038")
    sid = _publish(copy_svc, strategy_service, leader, "Followed One")
    copy_id = copy_svc.follow(sid, follower)["strategy_id"]

    def summary():
        return strategy_service.to_summary(strategy_service.get(copy_id))

    assert summary().leader_updated is False

    # the leader saves a new version → the follower's copy sees it
    code = CodeService(strategy_service.store, settings)
    code.save_code(sid, CodeUpdate(files={"main.py": "print('v2')\n"}))
    assert summary().leader_updated is True

    # pull = owner-only, then one click re-copies the code + deploys
    import pytest as _pytest
    with _pytest.raises(CopyError):
        copy_svc.pull_leader_update(copy_id, "not-the-owner")

    def fake_run(cmd, *args, **kwargs):
        if "venv" in cmd:
            from pathlib import Path
            bin_dir = Path(cmd[cmd.index("venv") + 1]) / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "python").write_text("#!/bin/sh\n")
            (bin_dir / "pip").write_text("#!/bin/sh\n")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr("app.services.deploy_service.subprocess.run", fake_run)
    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/python3")
    copy_svc.pull_leader_update(copy_id, follower)

    after = summary()
    assert after.leader_updated is False
    assert after.deployment_status == "deployed"
    doc = strategy_service.get(copy_id)
    import base64
    assert base64.b64decode(doc.code.files["main.py"]) == b"print('v2')\n"
    assert doc.copied_from is not None
    assert doc.copied_from.strategy_id == sid
