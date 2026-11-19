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


def test_list_searches_name_and_description(strategy_service):
    """Server-side search, case-insensitive, on name AND description —
    the Strategies page's search bar is a page of the matches."""
    from app.core.security import Principal
    strategy_service.create(StrategyCreate(
        name="BTST Stock Futs", description="daily overnight bet",
        files={"main.py": "print(1)"}), owner_id="u1")
    strategy_service.create(StrategyCreate(
        name="Breakout", description="intraday momentum",
        files={"main.py": "print(2)"}), owner_id="u1")
    principal = Principal("u1", "m1")

    hits = strategy_service.list(principal, query="btst")
    assert hits.total == 1
    assert hits.strategies[0].name == "BTST Stock Futs"
    # description match is also case-insensitive
    assert strategy_service.list(principal, query="MOMENTUM").total == 1
    # no match → an empty page, not an error
    miss = strategy_service.list(principal, query="zzz")
    assert miss.total == 0 and miss.count == 0
    # pagination runs over the MATCHES, not the whole set
    page = strategy_service.list(principal, query="o", page=1, page_size=1)
    assert page.total == 2 and page.count == 1 and page.pages == 2
