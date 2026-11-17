"""Quant Strategy Control Centre — FastAPI application.

Wires together the strategy, code, deployment, scheduler, process and log
services under the ``/api/v1`` base path. Also serves the built frontend
static assets when present.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import admin as admin_api
from app.api import auth as auth_api
from app.api import brokers as brokers_api
from app.api import code as code_api
from app.api import deploy as deploy_api
from app.api import logs as logs_api
from app.api import schedules as schedules_api
from app.api import strategies as strategies_api
from app.api import system as system_api
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, setup_platform_logging

log = get_logger("main")


def _startup_broker_scheduler() -> None:
    """Heal broker state and (re-)arm the daily token-refresh jobs.

    Deliberately non-fatal: a storage/secrets problem at startup must never
    prevent the API from serving.
    """
    try:
        from app.api.deps import (get_broker_scheduler, get_broker_service,
                                  get_tick_service)

        get_broker_service().reconcile_on_startup()
        get_broker_scheduler().start()
        get_tick_service().reconcile_on_startup()  # re-arm enabled tick feeds
    except Exception as exc:  # noqa: BLE001 — logged, never fatal
        log.warning("broker scheduler not started: %s", exc)


def _shutdown_broker_scheduler() -> None:
    try:
        from app.api.deps import get_broker_scheduler, get_tick_service

        get_tick_service().shutdown()  # stop feed workers cleanly
        get_broker_scheduler().shutdown()
    except Exception:  # noqa: BLE001
        pass


def _startup_strategy_scheduler() -> None:
    """Start the in-process strategy scheduler and rebuild its jobs from S3.

    Deliberately non-fatal: a scheduler/storage problem at startup must never
    prevent the API from serving (the startup reconcile rebuilds every job
    from the strategy documents — S3 is the source of truth).
    """
    try:
        from app.api.deps import get_scheduler_service
        from app.services.strategy_scheduler import shared_scheduler

        shared_scheduler().start()
        get_scheduler_service().reconcile()
    except Exception as exc:  # noqa: BLE001 — logged, never fatal
        log.warning("strategy scheduler not started: %s", exc)


def _shutdown_strategy_scheduler() -> None:
    try:
        from app.services.strategy_scheduler import shared_scheduler

        shared_scheduler().shutdown()
    except Exception:  # noqa: BLE001
        pass


def create_app() -> FastAPI:
    settings = get_settings()
    setup_platform_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _startup_broker_scheduler()
        _startup_strategy_scheduler()
        yield
        _shutdown_strategy_scheduler()
        _shutdown_broker_scheduler()

    app = FastAPI(
        title="Quant Strategy Control Centre",
        version=__version__,
        description="Manage, edit, deploy, schedule and monitor algorithmic trading strategies.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_hosts,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    base = settings.api_base_path
    # Public routes: signup / signin (the dashboard's login screen).
    app.include_router(auth_api.router, prefix=base)
    # Operator-only routes (audited admin actions — no ambient user-data access).
    app.include_router(admin_api.router, prefix=base)
    app.include_router(strategies_api.router, prefix=base)
    app.include_router(code_api.router, prefix=base)
    app.include_router(brokers_api.router, prefix=base)
    app.include_router(deploy_api.router, prefix=base)
    app.include_router(schedules_api.router, prefix=base)
    app.include_router(logs_api.router, prefix=base)
    app.include_router(system_api.router, prefix=base)

    @app.get("/health", tags=["ops"], include_in_schema=False)
    @app.get("/healthz", tags=["ops"], include_in_schema=False)
    def liveness():  # pragma: no cover
        """Public liveness probe for load balancers / process managers.

        Deliberately cheap and unauthenticated: no S3, scheduler or filesystem
        calls, so it reflects process liveness only. The richer, authenticated
        health view (storage, scheduler) stays at ``{base}/system/health``.
        """
        return {"status": "ok", "service": "quant-strategy-control-centre",
                "version": __version__}

    # Serve the built frontend if present. The mount is registered AFTER the
    # API routers (so /api/v1 keeps working) and REPLACES the root route —
    # otherwise "/" returns the JSON service banner instead of the
    # dashboard's index.html.
    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
    else:
        @app.get("/", tags=["root"])
        def root():  # pragma: no cover
            return {"service": "quant-strategy-control-centre", "version": __version__,
                    "docs": f"{base}", "health": f"{base}/system/health",
                    "liveness": "/health"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port,
                reload=os.environ.get("RELOAD", "0") == "1")
