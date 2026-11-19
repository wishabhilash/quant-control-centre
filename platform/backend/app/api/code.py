"""Code project endpoints: retrieve, save, ZIP upload (spec section 7)."""
from __future__ import annotations

import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.deps import get_code_service, get_store, get_strategy_service
from app.core.security import Principal, get_current_user, require_auth
from app.models.strategy import (
    CodeUpdate,
    DEFAULT_ENTRYPOINT,
    RuntimeFileContent,
    RuntimeFilesResponse,
)
from app.services.code_service import CodeService, CodeError
from app.services.strategy_service import StrategyService, StrategyServiceError
from app.storage.strategy_store import StrategyStore

router = APIRouter(prefix="/strategies", tags=["code"], dependencies=[Depends(require_auth)])


def _require_owner(strategy_id: str, principal: Principal,
                   svc: StrategyService) -> None:
    """Ownership check — a user may only touch their own strategy."""
    try:
        svc.get_for_user(strategy_id, principal)
    except StrategyServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN if "access" in str(exc)
                            else status.HTTP_404_NOT_FOUND, str(exc)) from exc


def _ensure_source_viewable(doc) -> None:
    """Copied strategies hide their source: the follower gets the
    behaviour, not the leader's IP (consent + IP stay with the original
    author).

    400, deliberately — not 403: the client logs out on 401/403 (identity
    problems), and a business denial here must NEVER kill the session —
    the same convention as change-password's wrong-current-password being
    a 400. A 403 here once looped the follower straight back to the
    sign-in screen on every successful login (the workspace re-fetched
    the code on mount)."""
    if getattr(doc, "copied_from", None) is not None:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=(
            "the source code of a copied strategy is not viewable — "
            "you control its runs, schedules and logs"))


def _require_source_editable(strategy_id: str, strategies) -> None:
    """Copied strategies hide their source: the follower controls the runs,
    schedules and logs — the leader's IP stays with them (edits and uploads
    are refused too)."""
    _ensure_source_viewable(strategies.get(strategy_id))


@router.get("/{strategy_id}/code")
def get_code(strategy_id: str, store: StrategyStore = Depends(get_store),
             principal: Principal = Depends(get_current_user),
             svc: StrategyService = Depends(get_strategy_service)) -> dict:
    _require_owner(strategy_id, principal, svc)
    try:
        doc = store.get_document(strategy_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_404_NOT_FOUND, "strategy not found") from exc
    _ensure_source_viewable(doc)
    return {
        "strategy_id": doc.strategy_id,
        "encoding": doc.code.encoding,
        "entrypoint": DEFAULT_ENTRYPOINT,
        # Return decoded text for the editor.
        "files": {p: StrategyStore.decode_file(b64) for p, b64 in doc.code.files.items()},
    }


@router.put("/{strategy_id}/code", status_code=status.HTTP_200_OK)
def save_code(strategy_id: str, update: CodeUpdate,
              principal: Principal = Depends(get_current_user),
              svc: CodeService = Depends(get_code_service),
              strategies: StrategyService = Depends(get_strategy_service)) -> dict:
    _require_source_editable(strategy_id, strategies)
    _require_owner(strategy_id, principal, strategies)
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


@router.get("/{strategy_id}/runtime-files", response_model=RuntimeFilesResponse)
def list_runtime_files(strategy_id: str,
                       principal: Principal = Depends(get_current_user),
                       svc: CodeService = Depends(get_code_service),
                       strategies: StrategyService = Depends(get_strategy_service)
                       ) -> RuntimeFilesResponse:
    """Files the strategy wrote into its own directory at run time — visible in
    the editor's read-only "runtime" section. Logs, the pid file and hidden
    entries are never listed (same policy as the content endpoint)."""
    _require_owner(strategy_id, principal, strategies)
    return RuntimeFilesResponse(strategy_id=strategy_id,
                                files=svc.list_runtime_files(strategy_id))


@router.get("/{strategy_id}/runtime-files/{rel_path:path}",
            response_model=RuntimeFileContent)
def read_runtime_file(rel_path: str, strategy_id: str,
                      principal: Principal = Depends(get_current_user),
                      svc: CodeService = Depends(get_code_service),
                      strategies: StrategyService = Depends(get_strategy_service)
                      ) -> RuntimeFileContent:
    """Content of one runtime file (text, or base64 for binary), capped and
    truncated. The path must stay inside the strategy directory."""
    _require_owner(strategy_id, principal, strategies)
    try:
        return svc.read_runtime_file(strategy_id, rel_path)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/{strategy_id}/upload", status_code=status.HTTP_201_CREATED)
async def upload_zip(strategy_id: str, file: UploadFile = File(...),
                    principal: Principal = Depends(get_current_user),
                    svc: CodeService = Depends(get_code_service),
                    strategies: StrategyService = Depends(get_strategy_service)) -> dict:
    _require_source_editable(strategy_id, strategies)
    _require_owner(strategy_id, principal, strategies)
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
