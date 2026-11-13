"""Application-level routes (root + public liveness probe).

Load balancers and process managers probe ``/health`` (or ``/healthz``) without
an admin token — the backend must answer those, cheaply and without auth, while
the richer health view stays authenticated under the API base path.
"""
from app.main import create_app


def _route(app, path):
    for r in app.routes:
        if getattr(r, "path", None) == path:
            return r
    return None


def test_liveness_probe_exists_and_is_public():
    app = create_app()
    for path in ("/health", "/healthz"):
        route = _route(app, path)
        assert route is not None, f"{path} must be exposed for load-balancer probes"
        # No auth dependency: a prober cannot present a Bearer token.
        assert not getattr(route, "dependencies", []), f"{path} must stay unauthenticated"


def test_liveness_probe_is_cheap_and_reports_ok():
    """It must not touch S3, the scheduler or the filesystem — process liveness only."""
    app = create_app()
    route = _route(app, "/health")
    payload = route.endpoint()
    assert payload["status"] == "ok"
    assert payload["service"] == "quant-strategy-control-centre"
    assert "version" in payload


def test_root_advertises_the_probe_paths():
    app = create_app()
    root = _route(app, "/")
    payload = root.endpoint()
    assert payload["liveness"] == "/health"
    assert payload["health"].endswith("/system/health")
