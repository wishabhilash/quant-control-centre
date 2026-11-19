"""Copy trading — the compliant "run the algo for the follower" model.

NOT signal-based mirroring (restricted in India): on FOLLOW, the platform
creates a **new strategy owned by the follower** — a deep copy of the
leader's project files at that moment — which the follower deploys, schedules
and controls like any strategy of their own, with THEIR broker tokens, THEIR
tick claims, THEIR kill switch and logs. The leader's source code is never
shown to the follower (code routes refuse; the workspace hides the tab) —
the follower gets the behaviour, not the IP.

Design decisions encoded here:

- **Frozen snapshot**: the copy freezes the leader's code at follow time;
  leader updates do NOT auto-propagate (the follower's live account must
  never change code beneath itself). The copied ``source_hash`` powers an
  "update available" indicator later.
- **Schedules as disabled drafts**: the leader's schedule definitions (cron,
  timezone, args) are copied with ``enabled=False`` — the follower gets the
  right cadence for the strategy's shape and arms each trigger themselves
  after deploy.
- **One index document** (``strategies/index.json``): the Marketplace reads
  ONE S3 object per view — S3 is a document store, not a relational DB, so
  a listing must never be N sequential reads. The index holds the published
  entries and the copy registry; it is a REBUILDABLE PROJECTION of the
  strategy documents (one full scan regenerates it), so golden rule 1 keeps
  a single source of truth. Rebuilt when missing/corrupt and at most hourly
  otherwise; every hot path (publish, follow, strategy delete, strategy
  update) updates it inline.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.strategy import CopiedFrom, Schedule, StrategyCreate
from app.services.strategy_service import StrategyService, StrategyServiceError
from app.services.user_service import UserService
from app.storage.strategy_store import StrategyStore

log = get_logger("services.copy")

INDEX_KEY = "strategies/index.json"
#: The projection self-heals: a full rescan at most this often (cosmetic
#: drift — a renamed strategy — is corrected within this window; hard
#: events like publish/follow/delete update the index inline).
REBUILD_AFTER_SECONDS = 3600.0


class CopyError(Exception):
    """A copy-trading domain error (the API maps it to 400)."""


class CopyService:
    def __init__(self, settings: Optional[Settings] = None,
                 strategy_service: Optional[StrategyService] = None,
                 user_service: Optional[UserService] = None,
                 store: Optional[StrategyStore] = None):
        self.settings = settings or get_settings()
        self.strategy_service = strategy_service or StrategyService()
        self.user_service = user_service or UserService()
        self.store = store or self.strategy_service.store

    # --- the index document --------------------------------------------------
    def _load_index(self) -> Dict[str, Any]:
        """The marketplace index — one S3 read. Missing/corrupt/STALE →
        rebuilt from the strategy documents (a full scan, bounded by the
        parallel store listing)."""
        try:
            raw = self.store.storage.get_object(INDEX_KEY)
            index = json.loads(raw.decode("utf-8"))
            built_at = index.get("built_at")
            if built_at:
                import datetime as dt
                built = dt.datetime.fromisoformat(built_at).timestamp()
                import time
                if time.time() - built > REBUILD_AFTER_SECONDS:
                    log.info("marketplace index stale (%.0fh) — rebuilding",
                             (time.time() - built) / 3600)
                    return self._rebuild_index()
            return index
        except Exception as exc:  # noqa: BLE001 — missing/corrupt → rebuild
            log.info("marketplace index unavailable (%s) — rebuilding",
                     type(exc).__name__)
            return self._rebuild_index()

    def _save_index(self, index: Dict[str, Any]) -> None:
        import datetime as dt
        index["updated_at"] = dt.datetime.now(dt.timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")
        self.store.storage.put_object(
            key=INDEX_KEY,
            body=json.dumps(index, indent=2).encode("utf-8"),
        )

    def _rebuild_index(self) -> Dict[str, Any]:
        """Regenerate the projection from the strategy documents (one full
        scan — bounded by the parallel store listing). The self-healing path
        for a missing/corrupt/stale index."""
        entries: List[Dict[str, Any]] = []
        copies: List[Dict[str, Any]] = []
        users = {u.user_id: u for u in self.user_service.store.list_users()}
        for doc in self.store.list_documents():
            if doc.copied_from is not None:
                copies.append({
                    "copy_id": doc.strategy_id,
                    "leader_id": doc.copied_from.strategy_id,
                    "owner_id": doc.owner_id or "",
                    "copied_at": doc.copied_from.copied_at,
                })
                continue  # copies are never published themselves
            if not doc.copy_trading:
                continue
            owner = users.get(doc.owner_id)
            entries.append({
                "strategy_id": doc.strategy_id,
                "name": doc.name,
                "description": doc.description,
                "owner_id": doc.owner_id or "",
                "owner_name": (owner.full_name if owner else None) or "Unknown",
                "author_deployed": doc.deployment.last_deployed_at is not None,
            })
        entries.sort(key=lambda s: s["name"])
        import datetime as dt
        index = {
            "schema_version": 1,
            "built_at": dt.datetime.now(dt.timezone.utc).replace(
                microsecond=0).isoformat().replace("+00:00", "Z"),
            "entries": entries,
            "copies": copies,
        }
        self._save_index(index)
        return index

    # --- leader side ---------------------------------------------------------
    def set_copy_trading(self, strategy_id: str, enabled: bool) -> Dict[str, Any]:
        """The LEADER opts a strategy in (or out) of the Marketplace."""
        doc = self.strategy_service.get(strategy_id)
        if doc.copied_from is not None:
            raise CopyError("a copied strategy cannot itself be offered for "
                            "copy trading")
        doc.copy_trading = bool(enabled)
        self.strategy_service.store.update_document(doc)

        index = self._load_index()
        index["entries"] = [e for e in index.get("entries", [])
                            if e["strategy_id"] != strategy_id]
        if enabled:
            owner = self.user_service.get_by_id(doc.owner_id or "")
            index["entries"].append({
                "strategy_id": doc.strategy_id,
                "name": doc.name,
                "description": doc.description,
                "owner_id": doc.owner_id or "",
                "owner_name": (owner.full_name if owner else None) or "Unknown",
                "author_deployed": doc.deployment.last_deployed_at is not None,
            })
            index["entries"].sort(key=lambda e: e["name"])
        self._save_index(index)
        return {"strategy_id": doc.strategy_id, "copy_trading": doc.copy_trading}

    def sync_entry(self, doc) -> None:
        """Keep the index entry current after a strategy update (rename,
        description edit). Best-effort: the hourly rebuild is the backstop."""
        try:
            index = self._load_index()
            index["entries"] = [e for e in index.get("entries", [])
                                if e["strategy_id"] != doc.strategy_id]
            if doc.copy_trading:
                owner = self.user_service.get_by_id(doc.owner_id or "")
                index["entries"].append({
                    "strategy_id": doc.strategy_id,
                    "name": doc.name,
                    "description": doc.description,
                    "owner_id": doc.owner_id or "",
                    "owner_name": (owner.full_name if owner else None)
                                  or "Unknown",
                    "author_deployed": doc.deployment.last_deployed_at is not None,
                })
                index["entries"].sort(key=lambda e: e["name"])
            self._save_index(index)
        except Exception as exc:  # noqa: BLE001 — cosmetic drift self-heals
            log.warning("marketplace entry sync failed for %s: %s",
                        doc.strategy_id, type(exc).__name__)

    def on_strategy_deleted(self, strategy_id: str) -> None:
        """Drop the deleted strategy's marketplace presence: its offering
        entry (if a leader) and any follower copies of it (if a copy)."""
        try:
            index = self._load_index()
            index["entries"] = [e for e in index.get("entries", [])
                                if e["strategy_id"] != strategy_id]
            removed = [c for c in index.get("copies", [])
                       if c["copy_id"] == strategy_id]
            index["copies"] = [c for c in index.get("copies", [])
                               if c["copy_id"] != strategy_id]
            self._save_index(index)
            if removed:
                log.info("marketplace: removed follower copies of deleted %s",
                         strategy_id)
        except Exception as exc:  # noqa: BLE001 — the rebuild self-heals
            log.warning("marketplace cleanup failed for %s: %s",
                        strategy_id, type(exc).__name__)

    # --- marketplace ---------------------------------------------------------
    def universe(self, viewer_user_id: Optional[str], query: Optional[str] = None,
                 page: int = 1, page_size: int = 50) -> Dict[str, Any]:
        """Strategies enabled for copy trading across accounts — INCLUDING
        the viewer's own (marked ``is_mine``: seeing your listing as others
        do, with the live follower count, is the creator's feedback loop).
        The UI never offers a follow button on your own. Metadata only: the
        source code stays private.

        ONE S3 read (the index document) + server-side search
        (name/owner/description, case-insensitive) + pagination. Follower
        counts are computed from the copy registry at serve time — never
        stored, so they cannot drift."""
        index = self._load_index()
        followers: Dict[str, int] = {}
        for c in index.get("copies", []):
            followers[c["leader_id"]] = followers.get(c["leader_id"], 0) + 1

        entries = []
        for e in index.get("entries", []):
            entries.append({
                "strategy_id": e["strategy_id"],
                "name": e["name"],
                "description": e["description"],
                "owner_name": e["owner_name"],
                "followers": followers.get(e["strategy_id"], 0),
                "author_deployed": e.get("author_deployed", False),
                "is_mine": bool(viewer_user_id
                                and e["owner_id"] == viewer_user_id),
                "is_followed": any(c["leader_id"] == e["strategy_id"]
                                   and c["owner_id"] == viewer_user_id
                                   for c in index.get("copies", [])),
            })
        entries.sort(key=lambda s: (-s["followers"], s["name"]))

        needle = (query or "").strip().lower()
        if needle:
            entries = [s for s in entries
                       if needle in s["name"].lower()
                       or needle in s["owner_name"].lower()
                       or needle in (s["description"] or "").lower()]
        total = len(entries)
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 50), 200))
        pages = max(1, -(-total // page_size))
        # no clamping: a page beyond the end is EMPTY (predictable)
        start = (page - 1) * page_size
        return {"universe": entries[start:start + page_size],
                "total": total, "page": page, "page_size": page_size,
                "pages": pages}

    # --- follow: create the follower's own copy ------------------------------
    def follow(self, strategy_id: str, follower_user_id: str) -> Dict[str, Any]:
        """Create the follower's OWN strategy — a deep copy of the leader's
        project, disabled, with the leader's schedules as DISABLED drafts
        and ``copied_from`` attribution. The follower then connects their
        broker, deploys and arms the schedules themselves (full control)."""
        source = self.strategy_service.get(strategy_id)
        if not source.copy_trading:
            raise CopyError(f"{strategy_id} is not offered for copy trading")
        if source.owner_id == follower_user_id:
            raise CopyError("you cannot follow your own strategy")
        # One copy per leader strategy per follower: a second follow would
        # double every cycle and fork the claims registry.
        index = self._load_index()
        for c in index.get("copies", []):
            if (c["leader_id"] == strategy_id
                    and c["owner_id"] == follower_user_id):
                raise CopyError(
                    f"you already follow {strategy_id} "
                    f"(your copy: {c['copy_id']})")

        files = self.strategy_service.code_service.decode_project(source.code)
        copy = self.strategy_service.create(StrategyCreate(
            name=f"{source.name} (copy)",
            description=(f"Copy of {source.name} ({strategy_id}). "
                         "Runs on YOUR broker with YOUR capital — the source "
                         "code stays with the original author."),
            enabled=False,
            parameters=dict(source.configuration.parameters),
            files=files,
        ), owner_id=follower_user_id)

        copy.copied_from = CopiedFrom(
            strategy_id=source.strategy_id,
            owner_id=source.owner_id or "",
            source_hash=source.deployment.source_hash,
            copied_at=self._now(),
        )
        # Schedules as DISABLED drafts: the right cadence for the strategy's
        # shape, armed by the follower — never silently live.
        used: List[str] = []
        for source_schedule in source.schedules:
            schedule_id = self._next_schedule_id(used)
            used.append(schedule_id)
            copy.schedules.append(Schedule(
                schedule_id=schedule_id,
                name=source_schedule.name,
                cron_expression=source_schedule.cron_expression,
                timezone=source_schedule.timezone,
                enabled=False,
                command=source_schedule.command,
            ))
        self.strategy_service.store.update_document(copy)

        # The copy enters the registry (follower counts recompute at serve).
        index["copies"].append({
            "copy_id": copy.strategy_id,
            "leader_id": source.strategy_id,
            "owner_id": follower_user_id,
            "copied_at": copy.copied_from.copied_at,
        })
        self._save_index(index)
        log.info("copy trading: %s followed %s → new strategy %s",
                 follower_user_id, strategy_id, copy.strategy_id)
        return self._describe(copy)

    def mine(self, user_id: str) -> List[Dict[str, Any]]:
        """The caller's followed strategies (their copies), each with its
        leader attribution and live status. ONE index read + one document
        read per copy — never a full scan."""
        index = self._load_index()
        mine_ids = [c["copy_id"] for c in index.get("copies", [])
                    if c["owner_id"] == user_id]
        out: List[Dict[str, Any]] = []
        for copy_id in mine_ids:
            try:
                doc = self.strategy_service.get(copy_id)
            except StrategyServiceError:
                continue  # deleted between the index read and now
            out.append(self._describe(doc))
        return sorted(out, key=lambda d: d["copied_at"], reverse=True)

    # --- helpers -------------------------------------------------------------
    def _describe(self, doc) -> Dict[str, Any]:
        source_name = None
        if doc.copied_from is not None:
            try:
                source_name = self.strategy_service.get(
                    doc.copied_from.strategy_id).name
            except StrategyServiceError:
                source_name = None
        return {
            "strategy_id": doc.strategy_id,
            "name": doc.name,
            "enabled": doc.enabled,
            "deployment_status": doc.deployment.status,
            "schedule_count": len(doc.schedules),
            "copied_from": doc.copied_from.model_dump() if doc.copied_from else None,
            "source_name": source_name,
            "copied_at": doc.copied_from.copied_at if doc.copied_from else None,
        }

    @staticmethod
    def _next_schedule_id(used: List[str]) -> str:
        n = 1
        while True:
            candidate = f"SCH-{n:03d}"
            if candidate not in used:
                return candidate
            n += 1

    @staticmethod
    def _now() -> str:
        return dt.datetime.now(dt.timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")
