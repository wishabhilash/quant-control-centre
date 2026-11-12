"""Strategy service tests (spec 19)."""
import pytest

from app.models.strategy import StrategyCreate, StrategyUpdate


def test_create_lists_and_updates(strategy_service):
    doc = strategy_service.create(StrategyCreate(name="Alpha", description="d", entrypoint="main.py",
                                                files={"main.py": "print('hi')"}))
    assert doc.strategy_id.startswith("STR-")
    listed = strategy_service.list()
    assert listed.count == 1
    updated = strategy_service.update(doc.strategy_id, StrategyUpdate(name="Alpha2"))
    assert updated.name == "Alpha2"


def test_delete_disables_and_removes(strategy_service):
    doc = strategy_service.create(StrategyCreate(name="Beta", entrypoint="main.py",
                                                files={"main.py": "print(1)"}))
    result = strategy_service.delete(doc.strategy_id)
    assert "document_deleted" in result["steps"]
    assert strategy_service.list().count == 0
