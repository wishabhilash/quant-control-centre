"""Application-level routes (root, static dashboard, public liveness probe).

Load balancers and process managers probe ``/health`` (or ``/healthz``) without
an admin token — the backend must answer those, cheaply and without auth, while
the richer health view stays authenticated under the API base path.

The built frontend is served by the backend itself at ``/`` (same origin as
the API); the JSON service banner is only used when no frontend bundle exists.
"""
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Mount

from app.main import create_app


def _routes(app, path):
    return [r for r in app.routes if getattr(r, "path", None) == path]


def _frontend_dist() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def test_liveness_probe_exists_and_is_public():
    app = create_app()
    for path in ("/health", "/healthz"):
        route = next((r for r in _routes(app, path) if isinstance(r, APIRoute)), None)
        assert route is not None, f"{path} must be exposed for load-balancer probes"
        # No auth dependency: a prober cannot present a Bearer token.
        assert not getattr(route, "dependencies", []), f"{path} must stay unauthenticated"


def test_liveness_probe_is_cheap_and_reports_ok():
    """It must not touch S3, the scheduler or the filesystem — process liveness only."""
    app = create_app()
    route = next(r for r in _routes(app, "/health") if isinstance(r, APIRoute))
    payload = route.endpoint()
    assert payload["status"] == "ok"
    assert payload["service"] == "quant-strategy-control-centre"
    assert "version" in payload


def _root_mounts(app):
    """The static dashboard mount (Starlette registers a root mount with path "")."""
    return [r for r in app.routes
            if isinstance(r, Mount) and getattr(r, "path", "") in ("", "/")]


def test_root_is_the_dashboard_when_the_frontend_is_built():
    """A JSON route on "/" would shadow the dashboard's index.html."""
    app = create_app()
    api_roots = [r for r in _routes(app, "/") if isinstance(r, APIRoute)]
    if _frontend_dist().exists():
        assert not api_roots, 'the "/" JSON route must not shadow the dashboard'
        assert _root_mounts(app), "the built frontend must be mounted at /"
    else:
        # No bundle (dev/CI): the JSON banner is the root.
        assert api_roots, "without a frontend bundle the root must answer with the service banner"


def test_root_banner_advertises_the_probe_paths():
    app = create_app()
    api_roots = [r for r in _routes(app, "/") if isinstance(r, APIRoute)]
    if not api_roots:
        return  # the dashboard owns "/" — nothing to assert
    payload = api_roots[0].endpoint()
    assert payload["liveness"] == "/health"
    assert payload["health"].endswith("/system/health")
