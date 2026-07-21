"""ROUTE-01 request-scoped preprocessor degradation coverage."""

import logging

import pytest

from core.outcomes import OutcomeStatus
from core.telemetry import METRIC_FALLBACK_ACTIVATED, get_telemetry
from query_core.query_context import QueryContext
from query_core.query_manager import QueryManager
from query_core.query_result import QueryResult
from query_core.query_route import QueryRoute
from query_core.query_types import QueryType
from query_handlers.semantic_search_handler import SemanticSearchHandler
from query_preprocessors import (
    BuildingExtractor,
    BusinessTermExtractor,
    QueryComplexityAnalyser,
    SpellCheckPreprocessor,
)


def _manager() -> QueryManager:
    manager = object.__new__(QueryManager)
    manager.logger = logging.getLogger("test_preprocessor_degradation")
    return manager


@pytest.mark.parametrize(
    ("preprocessor_type", "component"),
    [
        (SpellCheckPreprocessor, "spell_check_preprocessor"),
        (BuildingExtractor, "building_extractor"),
        (BusinessTermExtractor, "business_term_extractor"),
        (QueryComplexityAnalyser, "query_complexity_analyser"),
    ],
)
def test_each_preprocessor_has_a_stable_degradation_component(
    monkeypatch, preprocessor_type, component
):
    manager = _manager()
    preprocessor = preprocessor_type()
    manager.preprocessors = [preprocessor]
    monkeypatch.setattr(preprocessor, "should_run", lambda _context: True)
    monkeypatch.setattr(
        preprocessor,
        "process",
        lambda _context: (_ for _ in ()).throw(RuntimeError("private detail")),
    )

    context = QueryContext(query="BMS information")

    assert manager._run_preprocessors(context) == [component]


def test_failed_preprocessor_is_recorded_and_later_preprocessors_continue(
    monkeypatch,
):
    manager = _manager()
    building_extractor = BuildingExtractor()
    later_preprocessor = QueryComplexityAnalyser()
    manager.preprocessors = [building_extractor, later_preprocessor]
    later_ran = []

    monkeypatch.setattr(building_extractor, "should_run", lambda _context: True)
    monkeypatch.setattr(
        building_extractor,
        "process",
        lambda _context: (_ for _ in ()).throw(RuntimeError("private detail")),
    )
    monkeypatch.setattr(later_preprocessor, "should_run", lambda _context: True)
    monkeypatch.setattr(
        later_preprocessor, "process", lambda _context: later_ran.append(True)
    )

    context = QueryContext(query="BMS information for Senate House")
    components = manager._run_preprocessors(context)

    assert components == ["building_extractor"]
    assert context.get_from_cache("preprocessor_degradations") == components
    assert "preprocessor_degraded:building_extractor" in context.routing_notes
    assert later_ran == [True]
    assert (
        get_telemetry().get(
            METRIC_FALLBACK_ACTIVATED, component="building_extractor"
        )
        >= 1
    )


def test_material_preprocessor_failure_degrades_search_result():
    manager = _manager()
    context = QueryContext(query="BMS information for Senate House")
    result = QueryResult(
        query=context.query,
        answer="answer",
        query_type=QueryType.SEMANTIC_SEARCH.value,
        status=OutcomeStatus.SUCCESS,
    )

    manager._apply_preprocessor_degradation(
        result, context, ["building_extractor", "business_term_extractor"]
    )

    assert result.status is OutcomeStatus.DEGRADED
    assert result.degraded_components == [
        "building_extractor",
        "business_term_extractor",
    ]
    assert result.metadata["preprocessor_degradations"] == result.degraded_components


def test_non_material_preprocessor_failure_is_recorded_without_warning():
    manager = _manager()
    context = QueryContext(query="hello")
    result = QueryResult(
        query=context.query,
        answer="Hello!",
        query_type=QueryType.CONVERSATIONAL.value,
        status=OutcomeStatus.SUCCESS,
    )

    manager._apply_preprocessor_degradation(
        result,
        context,
        ["business_term_extractor", "query_complexity_analyser"],
    )

    assert result.status is OutcomeStatus.SUCCESS
    assert result.degraded_components == [
        "business_term_extractor",
        "query_complexity_analyser",
    ]


def test_existing_building_scope_makes_extractor_failure_non_material():
    manager = _manager()
    context = QueryContext(
        query="show BMS information",
        building_filter="Senate House",
    )
    result = QueryResult(
        query=context.query,
        answer="answer",
        query_type=QueryType.SEMANTIC_SEARCH.value,
        status=OutcomeStatus.SUCCESS,
    )

    manager._apply_preprocessor_degradation(
        result, context, ["building_extractor"]
    )

    assert result.status is OutcomeStatus.SUCCESS
    assert result.degraded_components == ["building_extractor"]


def test_process_query_attaches_material_preprocessor_degradation(monkeypatch):
    manager = QueryManager(intent_classifier=object())
    handler = SemanticSearchHandler()

    monkeypatch.setattr(
        "query_core.query_manager.missing_required_query_dependency", lambda: None
    )
    monkeypatch.setattr(
        manager,
        "_run_preprocessors",
        lambda _context: ["business_term_extractor"],
    )
    monkeypatch.setattr(
        manager, "_record_building_directory_readiness", lambda _context: False
    )
    monkeypatch.setattr(
        manager,
        "_route_query_hybrid",
        lambda _context: QueryRoute(handler=handler, metadata={"route": "test"}),
    )
    monkeypatch.setattr(
        handler,
        "handle",
        lambda context: QueryResult(query=context.query, answer="answer"),
    )
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

    result = manager.process_query(
        "BMS information",
        authenticated=True,
        tenant_id="tenant-test",
        user_roles=("reader",),
    )

    assert result.status is OutcomeStatus.DEGRADED
    assert result.degraded_components == ["business_term_extractor"]
    assert result.metadata["preprocessor_degradations"] == [
        "business_term_extractor"
    ]
