"""Storage tests: CRUD, base64, conditional writes, integrity (spec 19)."""
import base64
import pytest

from app.models.strategy import (
    CodeBlock, Configuration, Schedule, StrategyCreate, StrategyDocument,
)
from app.services.code_service import CodeService
from app.storage.strategy_store import StrategyStore


def _make_doc(sid="STR-001", entry="main.py"):
    return StrategyDocument(
        strategy_id=sid, name="Test", description="d", enabled=True,
        code=CodeBlock(encoding="base64",
                       files={"main.py": StrategyStore.encode_file("print('hi')"),
                              "utils.py": StrategyStore.encode_file("X=1")}),
        configuration=Configuration(parameters={"lots": 2}),
        schedules=[Schedule(schedule_id="SCH-001", name="Morning", cron_expression="20 9 * * 1-5",
                            timezone="Asia/Kolkata", enabled=True)],
    )


def test_create_and_retrieve(store):
    doc = _make_doc()
    store.create_document(doc)
    got = store.get_document("STR-001")
    assert got.name == "Test"
    assert set(got.code.files) == {"main.py", "utils.py"}


def test_base64_roundtrip():
    text = "hello 世界 🌍"
    assert StrategyStore.decode_file(StrategyStore.encode_file(text)) == text


def test_conditional_write_conflict(store):
    doc = _make_doc()
    store.create_document(doc)
    etag = store.get_etag("STR-001")
    doc.name = "Renamed"
    store.update_document(doc, if_match_etag=etag)  # ok
    with pytest.raises(Exception):
        # stale etag -> conflict
        store.update_document(doc, if_match_etag='"stale"')


def test_sha256_integrity(store):
    doc = _make_doc()
    h1 = store.document_hash(doc)
    doc2 = _make_doc()
    doc2.code.files["main.py"] = StrategyStore.encode_file("print('changed')")
    h2 = store.document_hash(doc2)
    assert h1 != h2


def test_delete(store):
    store.create_document(_make_doc())
    store.delete_document("STR-001")
    with pytest.raises(Exception):
        store.get_document("STR-001")


def test_unique_schedule_ids():
    with pytest.raises(Exception):
        StrategyDocument(
            strategy_id="STR-001", name="x",
            code=CodeBlock(files={"m.py": "Y"}),
            schedules=[Schedule(schedule_id="S1", name="a", cron_expression="* * * * *"),
                       Schedule(schedule_id="S1", name="b", cron_expression="* * * * *")],
        )


def test_legacy_document_loads_and_strips_deprecated_fields():
    """A previously-saved S3 doc may still carry the removed fields
    (code.entrypoint, runtime, configuration.initial_capital); the loader must
    tolerate them so old strategies keep loading (extra='forbid')."""
    legacy = {
        "strategy_id": "STR-009",
        "name": "Legacy",
        "enabled": True,
        "code": {"encoding": "base64", "entrypoint": "main.py", "files": {}},
        "configuration": {"initial_capital": 50000, "parameters": {"x": 1}},
        "runtime": {"python_version": "3.12", "timeout_seconds": 1800,
                    "allow_concurrent_runs": False},
        "schedules": [{"schedule_id": "SCH-001", "name": "m",
                       "cron_expression": "20 9 * * 1-5", "timezone": "Asia/Kolkata",
                       "enabled": True, "command": "--mode live"}],
    }
    doc = StrategyDocument.model_validate(legacy)
    assert not hasattr(doc, "runtime")
    assert "entrypoint" not in doc.code.model_dump()
    assert "initial_capital" not in doc.configuration.model_dump()
    assert doc.configuration.parameters == {"x": 1}
    # The new schedule command round-trips.
    assert doc.schedules[0].command == "--mode live"
