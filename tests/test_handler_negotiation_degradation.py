"""ROUTE-04 handler-negotiation degradation coverage."""

from types import SimpleNamespace

from core.outcomes import OutcomeStatus
from core.telemetry import METRIC_FALLBACK_ACTIVATED, get_telemetry
from query_core.query_manager import QueryManager
from query_core.query_result import QueryResult
from query_core.query_types import QueryType
from query_handlers import CountingHandler


class _StaticClassifier:
    def __init__(self, intent: QueryType, confidence: float):
        self.outcome = SimpleNamespace(intent=intent, confidence=confidence)

    def classify_intent(self, _query, _context):
        return self.outcome


def _stub_pipeline(monkeypatch, classifier: _StaticClassifier) -> QueryManager:
    manager = QueryManager(intent_classifier=classifier)
    monkeypatch.setattr(
        "query_core.query_manager.missing_required_query_dependency", lambda: None
    )
    monkeypatch.setattr(manager, "_run_preprocessors", lambda _context: [])
    monkeypatch.setattr(
        manager, "_record_building_directory_readiness", lambda _context: False
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

    for handler in manager.handlers:
        monkeypatch.setattr(
            handler,
            "handle",
            lambda context, selected=handler: QueryResult(
                query=context.query,
                answer="answer",
                query_type=selected.query_type.value,
            ),
        )
    manager.cache_enabled = True
    return manager


def _process(manager: QueryManager, query: str) -> QueryResult:
    return manager.process_query(
        query,
        authenticated=True,
        tenant_id="tenant-test",
        user_roles=("reader",),
    )


def test_rule_failure_that_outranks_fallback_degrades_result(monkeypatch):
    manager = _stub_pipeline(
        monkeypatch,
        _StaticClassifier(QueryType.SEMANTIC_SEARCH, confidence=0.1),
    )
    counting = next(
        handler for handler in manager.handlers if isinstance(handler, CountingHandler)
    )
    monkeypatch.setattr(
        counting,
        "can_handle",
        lambda _context: (_ for _ in ()).throw(RuntimeError("private detail")),
    )

    result = _process(manager, "Tell me about HVAC systems")

    assert result.status is OutcomeStatus.DEGRADED
    assert result.degraded_components == ["handler_negotiation"]
    assert result.metadata["handler_negotiation_failures"] == [
        {
            "handler": "counting",
            "phase": "rule",
            "authoritative": True,
        }
    ]
    assert "private detail" not in str(result.to_dict())
    assert manager.cache == {}
    assert (
        get_telemetry().get(
            METRIC_FALLBACK_ACTIVATED,
            component="counting",
        )
        >= 1
    )


def test_lower_priority_rule_failure_is_recorded_without_warning(monkeypatch):
    manager = _stub_pipeline(
        monkeypatch,
        _StaticClassifier(QueryType.SEMANTIC_SEARCH, confidence=0.1),
    )
    counting = next(
        handler for handler in manager.handlers if isinstance(handler, CountingHandler)
    )
    monkeypatch.setattr(
        counting,
        "can_handle",
        lambda _context: (_ for _ in ()).throw(RuntimeError("private detail")),
    )

    result = _process(manager, "Hello Alfred")

    assert result.status is OutcomeStatus.SUCCESS
    assert result.degraded_components == ["handler_negotiation"]
    assert result.metadata["handler_negotiation_failures"][0][
        "authoritative"
    ] is False
    assert manager.cache == {}


def test_ml_selected_handler_exception_is_material_and_distinct_from_rejection(
    monkeypatch,
):
    manager = _stub_pipeline(
        monkeypatch,
        _StaticClassifier(QueryType.COUNTING, confidence=0.99),
    )
    counting = next(
        handler for handler in manager.handlers if isinstance(handler, CountingHandler)
    )
    calls = 0

    def fail_during_ml_negotiation(_context):
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        raise RuntimeError("private detail")

    monkeypatch.setattr(counting, "can_handle", fail_during_ml_negotiation)

    result = _process(manager, "How many assets are recorded?")

    assert result.status is OutcomeStatus.DEGRADED
    assert result.metadata["handler_negotiation_failures"] == [
        {
            "handler": "counting",
            "phase": "ml_negotiation",
            "authoritative": True,
        }
    ]
    assert result.metadata["ml_route_reason"] == "handler_error"
    assert result.query_type == QueryType.SEMANTIC_SEARCH.value


def test_transient_rule_failure_is_non_material_when_same_handler_is_selected(
    monkeypatch,
):
    manager = _stub_pipeline(
        monkeypatch,
        _StaticClassifier(QueryType.COUNTING, confidence=0.99),
    )
    counting = next(
        handler for handler in manager.handlers if isinstance(handler, CountingHandler)
    )
    calls = 0

    def recover_during_ml_negotiation(_context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private detail")
        return True

    monkeypatch.setattr(counting, "can_handle", recover_during_ml_negotiation)

    result = _process(manager, "How many assets are recorded?")

    assert result.status is OutcomeStatus.SUCCESS
    assert result.query_type == QueryType.COUNTING.value
    assert result.metadata["handler_negotiation_failures"][0][
        "authoritative"
    ] is False


def test_normal_handler_rejection_is_not_a_failure(monkeypatch):
    manager = _stub_pipeline(
        monkeypatch,
        _StaticClassifier(QueryType.COUNTING, confidence=0.99),
    )
    counting = next(
        handler for handler in manager.handlers if isinstance(handler, CountingHandler)
    )
    monkeypatch.setattr(counting, "can_handle", lambda _context: False)

    result = _process(manager, "How many assets are recorded?")

    assert result.status is OutcomeStatus.SUCCESS
    assert result.metadata["ml_route_reason"] == "handler_rejected"
    assert "handler_negotiation_failures" not in result.metadata
    assert result.degraded_components == []
