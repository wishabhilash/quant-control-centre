"""Storage tests: CRUD, base64, conditional writes, integrity (spec 19)."""
import base64
import pytest

from app.models.strategy import (
    CodeBlock, Configuration, RuntimeSettings, Schedule, StrategyCreate, StrategyDocument,
)
from app.services.code_service import CodeService
from app.storage.strategy_store import StrategyStore


def _make_doc(sid="STR-001", entry="main.py"):
    return StrategyDocument(
        strategy_id=sid, name="Test", description="d", enabled=True,
        code=CodeBlock(encoding="base64", entrypoint=entry,
                       files={"main.py": StrategyStore.encode_file("print('hi')"),
                              "utils.py": StrategyStore.encode_file("X=1")}),
        configuration=Configuration(initial_capital=100000, parameters={"lots": 2}),
        runtime=RuntimeSettings(),
        schedules=[Schedule(schedule_id="SCH-001", name="Morning", cron_expression="20 9 * * 1-5",
                            timezone="Asia/Kolkata", enabled=True)],
    )


def test_create_and_retrieve(store):
    doc = _make_doc()
    store.create_document(doc)
    got = store.get_document("STR-001")
    assert got.name == "Test"
    assert got.code.entrypoint == "main.py"
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
            code=CodeBlock(entrypoint="m.py", files={"m.py": "Y"}),
            schedules=[Schedule(schedule_id="S1", name="a", cron_expression="* * * * *"),
                       Schedule(schedule_id="S1", name="b", cron_expression="* * * * *")],
        )
