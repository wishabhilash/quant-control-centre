#!/usr/bin/env python3
"""Generic platform runner invoked by Linux cron.

Cron entries look like::

    /opt/quant/platform/backend/venv/bin/python \
        /opt/quant/platform/backend/scripts/runner.py --schedule-id SCH-001

The runner locates the owning strategy, verifies enable/deploy/schedule state,
acquires an OS-level execution lock, launches the strategy's entry point with
the strategy's own virtual-environment interpreter, passes configuration via a
generated runtime config file, enforces a timeout and releases the lock.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Allow running the script without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.process_service import ProcessService  # noqa: E402
from app.services.scheduler_service import SchedulerService  # noqa: E402
from app.services.strategy_service import StrategyService  # noqa: E402


def find_schedule(schedule_id: str, strategy_service: StrategyService):
    for doc in strategy_service.store.list_documents():
        for s in doc.schedules:
            if s.schedule_id == schedule_id:
                return doc, s
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Generic strategy runner (cron-invoked).")
    parser.add_argument("--schedule-id", required=True)
    parser.add_argument("--manual", action="store_true", help="run manually (no schedule id needed if --strategy-id given)")
    parser.add_argument("--strategy-id", default=None)
    args = parser.parse_args()

    settings = get_settings()
    strategy_service = StrategyService(settings=settings)
    scheduler = SchedulerService(settings, strategy_service)

    doc, schedule = None, None
    if args.strategy_id:
        doc = strategy_service.get(args.strategy_id)
    else:
        doc, schedule = find_schedule(args.schedule_id, strategy_service)

    if doc is None:
        print(f"ERROR: no strategy/schedule found for {args.schedule_id}")
        return 2

    # (3) verify strategy enabled
    if not doc.enabled:
        print(f"SKIP: strategy {doc.strategy_id} is disabled")
        return 0
    # (4) verify deployed
    if doc.deployment.status != "deployed":
        print(f"SKIP: strategy {doc.strategy_id} is not deployed")
        return 0
    # (5) verify schedule enabled
    if schedule is not None and not schedule.enabled:
        print(f"SKIP: schedule {schedule.schedule_id} is disabled")
        return 0

    proc = ProcessService(settings, strategy_service)
    # (6) acquire lock + (7) launch + (10) timeout handled by start().
    try:
        info = proc.start(doc.strategy_id, schedule_id=args.schedule_id if schedule else "manual")
        print(f"STARTED pid={info.pid} strategy={doc.strategy_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP/ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
