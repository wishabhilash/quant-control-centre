"""Shared fixtures.

Uses a local filesystem-backed fake S3 so tests never touch real AWS.
"""
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("SCHEDULER_DRY_RUN", "true")


class FakeS3:
    """Minimal in-memory S3 stand-in with ETag support."""

    def __init__(self):
        self.objects = {}
        self._counter = 0

    def put_object(self, key, body, content_type=None, if_match_etag=None, **kw):
        if if_match_etag and self.objects.get(key, {}).get("etag") != if_match_etag:
            from app.storage.s3 import S3Error
            raise S3Error("concurrent modification: ETag mismatch")
        self._counter += 1
        etag = f'"etag-{self._counter:08d}"'
        self.objects[key] = {"body": body, "etag": etag}
        return etag

    def get_object(self, key, if_none_match_etag=None, **kw):
        if key not in self.objects:
            from app.storage.s3 import S3Error
            raise S3Error(f"object not found: {key}")
        return self.objects[key]["body"]

    def head_object(self, key, **kw):
        if key not in self.objects:
            from app.storage.s3 import S3Error
            raise S3Error(f"object not found: {key}")
        return {"ETag": self.objects[key]["etag"]}

    def get_etag(self, key):
        return self.objects.get(key, {}).get("etag")

    def delete_object(self, key, **kw):
        self.objects.pop(key, None)

    def list_objects(self, prefix="", **kw):
        out = []
        for k, v in self.objects.items():
            if k.startswith(prefix):
                out.append({"Key": k})
        return out

    def check_connectivity(self):
        return True


@pytest.fixture
def fake_s3():
    return FakeS3()


@pytest.fixture
def store(fake_s3):
    from app.storage.strategy_store import StrategyStore
    return StrategyStore(fake_s3)


@pytest.fixture
def settings():
    from app.core.config import Settings
    root = Path(tempfile.mkdtemp())
    s = Settings(
        environment="test",
        platform_root=root,
        scheduler_dry_run=True,
        auth_enabled=False,
    )
    s.strategies_dir = root / "strategies"
    s.runtime_dir = root / "runtime"
    s.locks_dir = root / "runtime" / "locks"
    s.state_dir = root / "runtime" / "state"
    for d in (s.strategies_dir, s.locks_dir, s.state_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s


@pytest.fixture
def strategy_service(store, settings):
    from app.services.strategy_service import StrategyService
    from app.services.code_service import CodeService
    return StrategyService(store=store, code_service=CodeService(store, settings), settings=settings)


@pytest.fixture
def scheduler_service(strategy_service, settings):
    from app.services.scheduler_service import SchedulerService
    return SchedulerService(settings, strategy_service)
