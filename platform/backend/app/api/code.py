"""Code project endpoints: retrieve, save, ZIP upload (spec section 7)."""
from __future__ import annotations

import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.deps import get_code_service, get_store
from app.core.security import require_auth
from app.models.strategy import CodeUpdate, DEFAULT_ENTRYPOINT
from app.services.code_service import CodeService, CodeError
from app.storage.strategy_store import StrategyStore

router = APIRouter(prefix="/strategies", tags=["code"], dependencies=[Depends(require_auth)])


@router.get("/{strategy_id}/code")
def get_code(strategy_id: str, store: StrategyStore = Depends(get_store)) -> dict:
    try:
        doc = store.get_document(strategy_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_404_NOT_FOUND, "strategy not found") from exc
    return {
        "strategy_id": doc.strategy_id,
        "encoding": doc.code.encoding,
        "entrypoint": DEFAULT_ENTRYPOINT,
        # Return decoded text for the editor.
        "files": {p: StrategyStore.decode_file(b64) for p, b64 in doc.code.files.items()},
    }


@router.put("/{strategy_id}/code", status_code=status.HTTP_200_OK)
def save_code(strategy_id: str, update: CodeUpdate,
              svc: CodeService = Depends(get_code_service)) -> dict:
    try:
        code = svc.save_code(strategy_id, update)
    except CodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if "not found" in str(exc).lower():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "strategy not found")
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"strategy_id": strategy_id, "entrypoint": DEFAULT_ENTRYPOINT,
            "files": sorted(code.files.keys()), "file_count": len(code.files)}


@router.post("/{strategy_id}/upload", status_code=status.HTTP_201_CREATED)
async def upload_zip(strategy_id: str, file: UploadFile = File(...),
                    svc: CodeService = Depends(get_code_service)) -> dict:
    data = await file.read()
    try:
        code, imported = svc.import_zip(strategy_id, data)
    except CodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if "not found" in str(exc).lower():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "strategy not found")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    return {"strategy_id": strategy_id, "entrypoint": DEFAULT_ENTRYPOINT,
            "imported_files": imported, "file_count": len(imported)}
