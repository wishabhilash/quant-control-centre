"""Log endpoints incl. WebSocket live streaming (spec section 13)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.api.deps import get_log_service, get_strategy_service
from app.core.security import Principal, get_current_user, require_auth
from app.services.log_service import LogService
from app.services.strategy_service import StrategyService, StrategyServiceError

router = APIRouter(prefix="/strategies", tags=["logs"], dependencies=[Depends(require_auth)])


def _require_owner(strategy_id: str, principal: Principal,
                   svc: StrategyService) -> None:
    try:
        svc.get_for_user(strategy_id, principal)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN if "access" in str(exc)
                            else status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/{strategy_id}/logs")
def get_logs(strategy_id: str,
             principal: Principal = Depends(get_current_user),
             svc: LogService = Depends(get_log_service),
             strategies: StrategyService = Depends(get_strategy_service),
             tail: int | None = Query(default=None, ge=1, le=100000),
             since: int | None = Query(default=None, ge=0),
             level: str | None = Query(default=None),
             search: str | None = Query(default=None)) -> dict:
    _require_owner(strategy_id, principal, strategies)
    return svc.get_lines(strategy_id, tail=tail, since=since, level=level, search=search)


@router.get("/{strategy_id}/logs/download")
def download_logs(strategy_id: str, principal: Principal = Depends(get_current_user),
                  svc: LogService = Depends(get_log_service),
                  strategies: StrategyService = Depends(get_strategy_service)) -> dict:
    _require_owner(strategy_id, principal, strategies)
    import io
    data = svc.download(strategy_id)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(io.BytesIO(data), media_type="text/plain",
                             headers={"Content-Disposition": f'attachment; filename="{strategy_id}.log"'})


@router.websocket("/{strategy_id}/logs/stream")
async def stream_logs(strategy_id: str, websocket: WebSocket,
                      svc: LogService = Depends(get_log_service),
                      strategies: StrategyService = Depends(get_strategy_service)):
    # The WebSocket carries the token as a query parameter (browsers cannot set
    # headers on a WebSocket handshake) — same ownership rules apply.
    token = websocket.query_params.get("token", "")
    principal = None
    try:
        from app.api.deps import get_user_store
        from app.core.config import get_settings
        from app.core.security import _principal_from_token
        principal = _principal_from_token(token, get_settings(), get_user_store())
        if principal is not None:
            strategies.get_for_user(strategy_id, principal)
    except Exception:  # noqa: BLE001
        principal = None
    if principal is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        async for msg in svc.stream(strategy_id, follow=True):
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        await websocket.send_json({"type": "error", "line": "stream ended"})
