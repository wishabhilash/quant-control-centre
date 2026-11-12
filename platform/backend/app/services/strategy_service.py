"""Strategy CRUD orchestration and summary generation.

Coordinates the document store, code service, scheduler and process managers to
implement the create/edit/delete workflows (spec sections 5, 6).
"""
from __future__ import annotations

from typing import List, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.strategy import (
    CodeBlock,
    Configuration,
    RuntimeSettings,
    Schedule,
    StrategyCreate,
    StrategyDocument,
    StrategyListResponse,
    StrategySummary,
    StrategyUpdate,
)
from app.services.code_service import CodeService
from app.storage.strategy_store import StrategyStore, StrategyNotFoundError

log = get_logger("services.strategy")


class StrategyServiceError(Exception):
    pass


class StrategyService:
    def __init__(
        self,
        store: Optional[StrategyStore] = None,
        code_service: Optional[CodeService] = None,
        settings: Optional[Settings] = None,
    ):
        self.store = store or StrategyStore()
        self.code_service = code_service or CodeService(self.store)
        self.settings = settings or get_settings()

    # --- create -----------------------------------------------------------
    def create(self, req: StrategyCreate) -> StrategyDocument:
        existing = self.store._list_strategy_ids()
        strategy_id = self.store.generate_strategy_id(existing)
        code = self.code_service.encode_project(req.files, req.entrypoint)
        doc = StrategyDocument(
            strategy_id=strategy_id,
            name=req.name,
            description=req.description,
            enabled=req.enabled,
            code=code,
            configuration=Configuration(initial_capital=req.initial_capital, parameters=req.parameters),
            runtime=req.runtime or RuntimeSettings(),
            schedules=[],
        )
        return self.store.create_document(doc)

    # --- read -------------------------------------------------------------
    def get(self, strategy_id: str) -> StrategyDocument:
        try:
            return self.store.get_document(strategy_id)
        except StrategyNotFoundError as exc:
            raise StrategyServiceError(str(exc)) from exc

    def list(self) -> StrategyListResponse:
        docs = self.store.list_documents()
        summaries = [self.to_summary(d) for d in docs]
        return StrategyListResponse(strategies=summaries, count=len(summaries))

    # --- update -----------------------------------------------------------
    def update(self, strategy_id: str, patch: StrategyUpdate, if_match_etag: Optional[str] = None) -> StrategyDocument:
        doc = self.get(strategy_id)
        enabled_changed = False
        if patch.name is not None:
            doc.name = patch.name
        if patch.description is not None:
            doc.description = patch.description
        if patch.enabled is not None:
            doc.enabled = patch.enabled
            enabled_changed = True
        if patch.runtime is not None:
            doc.runtime = patch.runtime
        updated = self.store.update_document(doc, if_match_etag=if_match_etag)
        # Toggling ``enabled`` changes whether the strategy's crons are active, so
        # keep the platform crontab in sync (reconcile installs/removes lines).
        if enabled_changed:
            sched = self._get_scheduler()
            if sched is not None:
                sched.reconcile(strategy_id)
        return updated

    # --- delete workflow (spec section 6) ---------------------------------
    def delete(self, strategy_id: str, force_stop: bool = False) -> dict:
        """Deletion workflow.

        1. disable cron schedules
        2. check running / stop if permitted
        3. ensure not managing live positions (guard — never auto-close)
        4. remove local files + venv
        5. delete S3 document

        Never cancels broker orders or closes broker positions automatically.
        """
        doc = self.get(strategy_id)
        steps = []

        # (1) disable schedules
        sched = self._get_scheduler()
        if sched is not None:
            for s in doc.schedules:
                sched.disable_schedule(strategy_id, s.schedule_id)
            sched.reconcile(strategy_id)
        steps.append("schedules_disabled")

        # (2) check running and stop if permitted
        proc = self._get_process_service()
        live_positions_guard = False
        if proc is not None:
            if proc.is_running(strategy_id):
                if force_stop:
                    proc.stop(strategy_id, graceful=True)
                    steps.append("stopped")
                else:
                    raise StrategyServiceError(
                        f"strategy {strategy_id} is running; pass force_stop to stop it first"
                    )
            live_positions_guard = proc.has_live_positions_guard(strategy_id)
            if live_positions_guard:
                log.warning("strategy %s has a live-positions guard; refusing to delete", strategy_id)
                raise StrategyServiceError("strategy manages live positions; resolve broker state before deleting")
        steps.append("live_positions_checked")

        # (4) remove local files + venv
        if proc is not None:
            proc.remove_local_resources(strategy_id)
        steps.append("local_resources_removed")

        # (5) delete S3 document
        self.store.delete_document(strategy_id)
        steps.append("document_deleted")

        log.info("deleted strategy %s steps=%s", strategy_id, steps)
        return {"strategy_id": strategy_id, "steps": steps,
                "live_positions_guard": live_positions_guard}

    # --- helpers ----------------------------------------------------------
    def to_summary(self, doc: StrategyDocument) -> StrategySummary:
        return StrategySummary(
            strategy_id=doc.strategy_id,
            name=doc.name,
            description=doc.description,
            enabled=doc.enabled,
            deployment_status=doc.deployment.status,
            schedule_count=len(doc.schedules),
            next_scheduled_execution=self._next_execution(doc),
            last_deployed_at=doc.deployment.last_deployed_at,
            updated_at=doc.updated_at,
        )

    def _next_execution(self, doc: StrategyDocument) -> Optional[str]:
        sched = self._get_scheduler()
        if sched is None:
            return None
        nexts = [sched.next_execution(s) for s in doc.schedules if s.enabled]
        return min([n for n in nexts if n], default=None)

    def _get_scheduler(self):
        try:
            from app.services.scheduler_service import SchedulerService
            # Share this strategy service (and its store) so reconcile() operates
            # on the same documents we are editing.
            return SchedulerService(self.settings, strategy_service=self)
        except Exception:  # noqa: BLE001
            return None

    def _get_process_service(self):
        try:
            from app.services.process_service import ProcessService
            return ProcessService(self.settings)
        except Exception:  # noqa: BLE001
            return None
