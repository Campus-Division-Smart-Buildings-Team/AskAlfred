"""ROUTE-05 intent-classifier degradation coverage."""

from types import SimpleNamespace

from core.outcomes import OutcomeStatus
from query_core.intent_classifier import IntentClassificationResult, NLPIntentClassifier
from query_core.query_manager import QueryManager
from query_core.query_result import QueryResult
from query_core.query_types import QueryType


class _RaisingClassifier:
    def classify_intent(self, _query, _context):
        raise RuntimeError("private classifier detail")


class _DegradedClassifier:
    def classify_intent(self, _query, _context):
        return SimpleNamespace(
            intent=QueryType.SEMANTIC_SEARCH,
            confidence=0.1,
            metadata={"degraded_reason": "model_unavailable"},
        )


def _stub_pipeline(monkeypatch, classifier) -> QueryManager:
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


def test_classifier_exception_degrades_semantic_fallback_without_leaking_details(
    monkeypatch,
):
    manager = _stub_pipeline(monkeypatch, _RaisingClassifier())

    result = _process(manager, "Tell me about unusual ventilation details")

    assert result.status is OutcomeStatus.DEGRADED
    assert result.degraded_components == ["intent_classifier"]
    assert result.metadata["intent_classifier_degradation"] == {
        "reason": "classification_error",
        "fallback": "semantic",
    }
    assert result.metadata["ml_route_reason"] == "classifier_error"
    assert "private classifier detail" not in str(result.to_dict())
    assert manager.cache == {}


def test_classifier_exception_degrades_rule_fallback(monkeypatch):
    manager = _stub_pipeline(monkeypatch, _RaisingClassifier())

    result = _process(manager, "Hello Alfred")

    assert result.status is OutcomeStatus.DEGRADED
    assert result.degraded_components == ["intent_classifier"]
    assert result.metadata["intent_classifier_degradation"] == {
        "reason": "classification_error",
        "fallback": "rule",
    }
    assert result.metadata["ml_route_reason"] == "classifier_error_rule_fallback"
    assert manager.cache == {}


def test_pattern_only_classifier_degradation_is_attached_to_result(monkeypatch):
    manager = _stub_pipeline(monkeypatch, _DegradedClassifier())

    result = _process(manager, "Tell me about unusual ventilation details")

    assert result.status is OutcomeStatus.DEGRADED
    assert result.degraded_components == ["intent_classifier"]
    assert result.metadata["intent_classifier_degradation"] == {
        "reason": "model_unavailable",
        "fallback": "pattern",
    }
    assert manager.cache == {}


def test_classifier_exposes_stable_reason_for_swallowed_semantic_error(monkeypatch):
    classifier = NLPIntentClassifier(enable_model=False)
    classifier.enabled = True
    monkeypatch.setattr(
        classifier,
        "_semantic_intent",
        lambda _query: (_ for _ in ()).throw(RuntimeError("private model detail")),
    )

    result = classifier.classify_intent("Unusual ventilation narrative")

    assert result.metadata["degraded_reason"] == "semantic_classification_error"
    assert "private model detail" not in str(result.metadata)


def test_low_semantic_confidence_remains_normal_routing(monkeypatch):
    classifier = NLPIntentClassifier(enable_model=False)
    classifier.enabled = True
    monkeypatch.setattr(
        classifier,
        "_semantic_intent",
        lambda _query: IntentClassificationResult(
            intent=QueryType.SEMANTIC_SEARCH,
            confidence=0.1,
        ),
    )

    result = classifier.classify_intent("Unusual ventilation narrative")

    assert result.method == "pattern"
    assert "degraded_reason" not in result.metadata


def test_pattern_only_mode_keeps_high_precision_fast_path_healthy():
    classifier = NLPIntentClassifier(enable_model=False)

    result = classifier.classify_intent("hello")

    assert "degraded_reason" not in result.metadata
