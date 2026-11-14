"""Administrative endpoints (operator only).

The operator's token is deliberately **not** a key to user data: it cannot
read, edit or run another user's strategies. What it *can* do is the explicit,
audited administrative work:

* list the platform's user accounts (to find a mobile number),
* see the strategies that belong to nobody (created before user accounts
  existed) and assign them to a user.

Every action here is logged, so "who looked at what" has an answer.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_strategy_service, get_user_service
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import require_admin
from app.models.user import AdminUser, PublicUser
from app.models.strategy import StrategySummary
from app.services.strategy_service import StrategyService, StrategyServiceError
from app.services.user_service import AuthError, UserService

log = get_logger("api.admin")
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/users", response_model=list[AdminUser])
def list_users(svc: UserService = Depends(get_user_service),
               strategies: StrategyService = Depends(get_strategy_service)) -> list[AdminUser]:
    """The platform's user accounts (id, name, mobile, status) with counts."""
    counts = strategies.count_by_owner()
    return [_admin_user(u, counts.get(u.user_id, 0)) for u in svc.list_users()]


def _admin_user(record, strategy_count: int = 0) -> AdminUser:
    return AdminUser(
        user_id=record.user_id,
        full_name=record.full_name,
        mobile=record.mobile,
        status=record.status,
        created_at=record.created_at,
        last_login_at=record.last_login_at,
        blocked_at=record.blocked_at,
        blocked_by=record.blocked_by,
        strategy_count=strategy_count,
    )


@router.post("/users/{user_id}/block", response_model=AdminUser)
def block_user(user_id: str, svc: UserService = Depends(get_user_service),
               strategies: StrategyService = Depends(get_strategy_service)) -> AdminUser:
    """Suspend an account. Instant: sign-in is refused and every session token
    the account holds stops verifying at once. Audited."""
    try:
        record = svc.block(user_id)
    except AuthError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _admin_user(record, strategies.count_by_owner().get(user_id, 0))


@router.post("/users/{user_id}/unblock", response_model=AdminUser)
def unblock_user(user_id: str, svc: UserService = Depends(get_user_service),
                 strategies: StrategyService = Depends(get_strategy_service)) -> AdminUser:
    """Restore a suspended account. Audited."""
    try:
        record = svc.unblock(user_id)
    except AuthError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _admin_user(record, strategies.count_by_owner().get(user_id, 0))


@router.get("/strategies/unassigned", response_model=list[StrategySummary])
def list_unassigned(svc: StrategyService = Depends(get_strategy_service)) -> list[StrategySummary]:
    """Strategies that belong to nobody — they can be claimed by a user."""
    return [svc.to_summary(d) for d in svc.list_unassigned()]

@router.post("/strategies/{strategy_id}/assign")
def assign_strategy(strategy_id: str, body: dict,
                    svc: StrategyService = Depends(get_strategy_service),
                    users: UserService = Depends(get_user_service)) -> dict:
    """Assign a strategy to a user (``{"mobile": "9876543210"}``) or unassign it
    (``{"mobile": null}``). Audited."""
    mobile = (body.get("mobile") or "").strip()
    owner_id = None
    if mobile:
        record = users.get_by_mobile(mobile)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"no user with mobile {mobile}")
        owner_id = record.user_id
    try:
        doc = svc.assign_owner(strategy_id, owner_id)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    log.info("ADMIN assigned strategy %s to %s", strategy_id, owner_id or "(unassigned)")
    return {"strategy_id": doc.strategy_id, "owner_id": doc.owner_id}
