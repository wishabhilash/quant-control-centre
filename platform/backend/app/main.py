"""Quant Strategy Control Centre — FastAPI application.

Wires together the strategy, code, deployment, scheduler, process and log
services under the ``/api/v1`` base path. Also serves the built frontend
static assets when present.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import code as code_api
from app.api import deploy as deploy_api
from app.api import logs as logs_api
from app.api import schedules as schedules_api
from app.api import strategies as strategies_api
from app.api import system as system_api
from app.core.config import get_settings
from app.core.logging import setup_platform_logging


def create_app() -> FastAPI:
    settings = get_settings()
    setup_platform_logging(settings.log_level)

    app = FastAPI(
        title="Quant Strategy Control Centre",
        version=__version__,
        description="Manage, edit, deploy, schedule and monitor algorithmic trading strategies.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_hosts,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    base = settings.api_base_path
    app.include_router(strategies_api.router, prefix=base)
    app.include_router(code_api.router, prefix=base)
    app.include_router(deploy_api.router, prefix=base)
    app.include_router(schedules_api.router, prefix=base)
    app.include_router(logs_api.router, prefix=base)
    app.include_router(system_api.router, prefix=base)

    @app.get("/", tags=["root"])
    def root():  # pragma: no cover
        return {"service": "quant-strategy-control-centre", "version": __version__,
                "docs": f"{base}", "health": f"{base}/system/health"}

    # Serve built frontend if present.
    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port,
                reload=os.environ.get("RELOAD", "0") == "1")
