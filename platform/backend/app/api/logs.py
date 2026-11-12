"""Log endpoints incl. WebSocket live streaming (spec section 13)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from app.api.deps import get_log_service
from app.core.security import require_auth
from app.services.log_service import LogService

router = APIRouter(prefix="/strategies", tags=["logs"], dependencies=[Depends(require_auth)])


@router.get("/{strategy_id}/logs")
def get_logs(strategy_id: str,
             svc: LogService = Depends(get_log_service),
             tail: int | None = Query(default=None, ge=1, le=100000),
             since: int | None = Query(default=None, ge=0),
             level: str | None = Query(default=None),
             search: str | None = Query(default=None)) -> dict:
    return svc.get_lines(strategy_id, tail=tail, since=since, level=level, search=search)


@router.get("/{strategy_id}/logs/download")
def download_logs(strategy_id: str, svc: LogService = Depends(get_log_service)) -> dict:
    import io
    data = svc.download(strategy_id)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(io.BytesIO(data), media_type="text/plain",
                             headers={"Content-Disposition": f'attachment; filename="{strategy_id}.log"'})


@router.websocket("/{strategy_id}/logs/stream")
async def stream_logs(strategy_id: str, websocket: WebSocket,
                      svc: LogService = Depends(get_log_service)):
    await websocket.accept()
    try:
        async for msg in svc.stream(strategy_id, follow=True):
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        await websocket.send_json({"type": "error", "line": "stream ended"})
