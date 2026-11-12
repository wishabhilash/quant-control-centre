#!/usr/bin/env python3
"""Generic platform runner invoked by Linux cron.

Cron entries look like::

    /opt/quant/platform/backend/venv/bin/python \
        /opt/quant/platform/backend/scripts/runner.py \
        --strategy-id STR-001 --schedule-id SCH-001 \
        >> /opt/quant/strategies/STR-001/runner.log 2>&1

The runner locates the owning strategy (and, when given, the specific schedule
within it), verifies enable/deploy/schedule state, acquires an OS-level
execution lock, launches the strategy's fixed entry point (``main.py``) with
the strategy's own virtual-environment interpreter, passing the schedule's
``command`` (read from S3 — the source of truth) as arguments, plus a generated
runtime config file, enforces a timeout and releases the lock.
The ``command`` string is split into an argument array (no shell).
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

# cron(1) runs with an empty-ish environment and a CWD of $HOME, so
# ``Settings`` would not find ``platform/backend/.env`` (its env_file is
# CWD-relative) and would fall back to real AWS with placeholder credentials.
# Load it explicitly from the script's own directory.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

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
    parser.add_argument("--strategy-id", default=None)
    parser.add_argument("--schedule-id", default=None,
                        help="schedule whose state/command governs this run "
                             "(omit for a manual run)")
    parser.add_argument("--manual", action="store_true",
                        help="run manually (no schedule state check)")
    # Optional override for the arguments passed to the entry point. When a
    # schedule is matched, its own ``command`` (from S3) is used unless given.
    parser.add_argument("--command", default=None,
                        help="override the schedule's command (args to the entry point)")
    args = parser.parse_args()

    settings = get_settings()
    strategy_service = StrategyService(settings=settings)
    scheduler = SchedulerService(settings, strategy_service)

    doc, schedule = None, None
    if args.strategy_id:
        doc = strategy_service.get(args.strategy_id)
        if args.schedule_id and not args.manual:
            # The schedule must be looked up *within* the given strategy:
            # SCH-NNN ids are only unique per strategy, not globally.
            schedule = next(
                (s for s in doc.schedules if s.schedule_id == args.schedule_id), None
            )
    else:
        doc, schedule = find_schedule(args.schedule_id, strategy_service)

    if doc is None:
        print(f"ERROR: no strategy found for --strategy-id {args.strategy_id} "
              f"(schedule: {args.schedule_id})")
        return 2
    if args.schedule_id and not args.manual and schedule is None:
        print(f"ERROR: schedule {args.schedule_id} not found on strategy "
              f"{doc.strategy_id}")
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

    # The arguments passed to the entry point come from the schedule's ``command``
    # (read from S3 — the source of truth) unless explicitly overridden.
    command = args.command if args.command is not None else (schedule.command if schedule else "")

    proc = ProcessService(settings, strategy_service)
    # (6) acquire lock + (7) launch + (10) timeout handled by start().
    try:
        info = proc.start(doc.strategy_id, schedule_id=args.schedule_id if schedule else "manual",
                          command=command)
        print(f"STARTED pid={info.pid} strategy={doc.strategy_id} args={command!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP/ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
