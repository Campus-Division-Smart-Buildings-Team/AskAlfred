"""ROUTE-02 discarded-building-scope behavioral coverage."""

import logging

import pytest

from core.failure_codes import FailureCode
from core.outcomes import OutcomeStatus
from core.telemetry import METRIC_FALLBACK_ACTIVATED, get_telemetry
from query_core.query_context import QueryContext
from query_core.query_manager import QueryManager
from query_core.query_result import QueryResult
from query_core.query_route import QueryRoute
from query_handlers.semantic_search_handler import SemanticSearchHandler
from ui.error_presenter import present_query_failure


def _bare_manager() -> QueryManager:
    manager = object.__new__(QueryManager)
    manager.logger = logging.getLogger("test_discarded_building_scope")
    return manager


def _stub_session(monkeypatch) -> None:
    monkeypatch.setattr(
        "query_core.query_manager.SessionManager.get_last_query_context",
        lambda: None,
    )
    monkeypatch.setattr(
        "query_core.query_manager.SessionManager.get_last_intent",
        lambda: (None, None),
    )
    monkeypatch.setattr(
        "query_core.query_manager.SessionManager.set_last_query_context",
        lambda _context: None,
    )
    monkeypatch.setattr(
        "query_core.query_manager.SessionManager.set_last_intent",
        lambda _intent, _confidence: None,
    )


def test_invalid_extracted_scope_is_recorded_cleared_and_non_material():
    manager = _bare_manager()
    context = QueryContext(query="Show maintenance requests")
    context.building = "maintenance"
    context.building_filter = "maintenance"
    context.buildings = ["maintenance"]

    outcome = manager._discard_invalid_building_scope(
        context, requested_building_filter=None
    )

    assert outcome == (True, False)
    assert context.building is None
    assert context.building_filter is None
    assert context.buildings == []
    assert context.get_from_cache("building_scope_discarded") is True
    assert "building_scope_discarded" in context.routing_notes
    assert (
        get_telemetry().get(
            METRIC_FALLBACK_ACTIVATED,
            component="building_scope_discarded",
        )
        >= 1
    )


def test_candidate_rejected_inside_extractor_is_still_recorded():
    manager = _bare_manager()
    context = QueryContext(query="Show maintenance requests")
    context.add_to_cache("building_invalid_candidate", "maintenance")

    assert manager._discard_invalid_building_scope(
        context, requested_building_filter=None
    ) == (True, False)
    assert context.get_from_cache("building_scope_discarded") is True


@pytest.mark.parametrize(
    "query",
    [
        "Show BMS at maintenance",
        "Show BMS within the maintenance",
        "Show BMS for the maintenance building",
        "Show BMS for the building named maintenance",
    ],
)
def test_explicit_natural_language_scope_requires_clarification(query):
    manager = _bare_manager()
    context = QueryContext(query=query)
    context.building = "maintenance"
    context.building_filter = "maintenance"

    assert manager._discard_invalid_building_scope(
        context, requested_building_filter=None
    ) == (True, True)


def test_invalid_explicit_filter_requires_clarification():
    manager = _bare_manager()
    context = QueryContext(
        query="Show BMS information",
        building_filter="maintenance",
    )

    assert manager._discard_invalid_building_scope(
        context, requested_building_filter="maintenance"
    ) == (True, True)
    assert context.building_filter is None


def test_discard_does_not_clear_a_separate_valid_explicit_filter():
    manager = _bare_manager()
    context = QueryContext(
        query="Show maintenance requests for Senate House",
        building_filter="Senate House",
    )
    context.building = "maintenance"
    context.buildings = ["maintenance"]

    assert manager._discard_invalid_building_scope(
        context, requested_building_filter="Senate House"
    ) == (True, False)
    assert context.building is None
    assert context.building_filter == "Senate House"


def test_material_discard_returns_clarification_before_routing(monkeypatch):
    manager = QueryManager(intent_classifier=object())
    _stub_session(monkeypatch)
    monkeypatch.setattr(
        "query_core.query_manager.missing_required_query_dependency", lambda: None
    )

    def discard_scope(context):
        context.building = "maintenance"
        context.building_filter = "maintenance"
        return []

    monkeypatch.setattr(manager, "_run_preprocessors", discard_scope)
    monkeypatch.setattr(
        manager,
        "_route_query_hybrid",
        lambda _context: (_ for _ in ()).throw(
            AssertionError("routing must not run before clarification")
        ),
    )

    result = manager.process_query(
        "Show BMS at maintenance",
        authenticated=True,
        tenant_id="tenant-test",
        user_roles=("reader",),
    )

    assert result.status is OutcomeStatus.REJECTED
    assert result.failure is not None
    assert result.failure.code is FailureCode.INPUT_BUILDING_SCOPE_INVALID
    assert result.failure.retryable is False
    assert result.metadata == {
        "building_scope_discarded": True,
        "clarification_required": True,
    }
    presented = present_query_failure(result)
    assert "building" in presented.message.lower()
    assert "building name" in presented.action.lower()


def test_non_material_discard_continues_and_marks_result(monkeypatch):
    manager = QueryManager(intent_classifier=object())
    handler = SemanticSearchHandler()
    handled_contexts = []
    _stub_session(monkeypatch)
    monkeypatch.setattr(
        "query_core.query_manager.missing_required_query_dependency", lambda: None
    )

    def discard_scope(context):
        context.building = "maintenance"
        context.building_filter = "maintenance"
        return []

    monkeypatch.setattr(manager, "_run_preprocessors", discard_scope)
    monkeypatch.setattr(
        manager, "_record_building_directory_readiness", lambda _context: False
    )
    monkeypatch.setattr(
        manager,
        "_route_query_hybrid",
        lambda _context: QueryRoute(handler=handler, metadata={"route": "test"}),
    )

    def handle(context):
        handled_contexts.append(context)
        return QueryResult(query=context.query, answer="answer")

    monkeypatch.setattr(handler, "handle", handle)

    result = manager.process_query(
        "Show maintenance requests",
        authenticated=True,
        tenant_id="tenant-test",
        user_roles=("reader",),
    )

    assert result.status is OutcomeStatus.SUCCESS
    assert result.metadata["building_scope_discarded"] is True
    assert handled_contexts[0].building is None
    assert handled_contexts[0].building_filter is None
