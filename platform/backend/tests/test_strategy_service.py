"""Strategy service tests (spec 19)."""
import pytest

from app.models.strategy import StrategyCreate, StrategyUpdate


def test_create_lists_and_updates(strategy_service):
    doc = strategy_service.create(StrategyCreate(name="Alpha", description="d",
                                                files={"main.py": "print('hi')"}))
    assert doc.strategy_id.startswith("STR-")
    listed = strategy_service.list()
    assert listed.count == 1
    updated = strategy_service.update(doc.strategy_id, StrategyUpdate(name="Alpha2"))
    assert updated.name == "Alpha2"


def test_delete_disables_and_removes(strategy_service):
    doc = strategy_service.create(StrategyCreate(name="Beta",
                                                files={"main.py": "print(1)"}))
    result = strategy_service.delete(doc.strategy_id)
    assert "document_deleted" in result["steps"]
    assert strategy_service.list().count == 0


def test_update_configuration(strategy_service):
    from app.models.strategy import Configuration
    doc = strategy_service.create(StrategyCreate(name="Gamma",
                                                files={"main.py": "print(3)"}))
    updated = strategy_service.update(
        doc.strategy_id,
        StrategyUpdate(configuration=Configuration(parameters={"x": 1})),
    )
    assert updated.configuration.parameters == {"x": 1}

    # A subsequent name-only update must NOT clobber configuration.
    again = strategy_service.update(doc.strategy_id, StrategyUpdate(name="Gamma2"))
    assert again.name == "Gamma2"
    assert again.configuration.parameters == {"x": 1}
