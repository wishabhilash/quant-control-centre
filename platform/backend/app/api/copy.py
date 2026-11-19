"""Copy-trading routes: the Universe (strategies offered across accounts)
and the follow flow (creates the follower's own copy — compliant with the
"run the algo for the user" model, NOT signal-based mirroring).

The follow/enable routes live on the strategy-scoped router (they act on
one strategy); only the cross-account Universe read lives here.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_copy_service
from app.core.security import Principal, get_current_user
from app.services.copy_service import CopyError
from app.api.deps import get_copy_service

router = APIRouter(prefix="/copy-trading", tags=["copy-trading"],
                   dependencies=[Depends(get_current_user)])


@router.get("/mine")
def mine(principal: Principal = Depends(get_current_user),
         svc: CopyService = Depends(get_copy_service)) -> dict:
    """The caller's followed strategies — their copies, with leader
    attribution and live status. One index read + one document read per
    copy (never a full scan)."""
    return {"mine": svc.mine(principal.user_id)}


@router.get("/universe")
def universe(query: Optional[str] = None, page: int = 1, page_size: int = 50,
             principal: Principal = Depends(get_current_user),
             svc: CopyService = Depends(get_copy_service)) -> dict:
    """Strategies enabled for copy trading across accounts (minus the
    caller's own — those carry the ``is_mine`` marker instead). Server-side
    search (name / owner / description) + pagination: the Marketplace stays
    usable at hundreds of published strategies. Metadata only — the source
    code stays with the leader."""
    return svc.universe(principal.user_id, query=query, page=page,
                        page_size=page_size)
