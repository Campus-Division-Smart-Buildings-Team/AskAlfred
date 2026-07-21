"""ROUTE-10 conversation-memory degradation coverage."""

from types import SimpleNamespace

from core.outcomes import OutcomeStatus
from core.session_manager import SessionManager
from core.telemetry import (
    COMPONENT_CONVERSATION_MEMORY,
    METRIC_FALLBACK_ACTIVATED,
    get_telemetry,
)
from query_core.query_manager import QueryManager
from query_core.query_result import QueryResult
from query_core.query_types import QueryType
from ui.error_presenter import (
    present_query_failure,
    query_degradation_notice_required,
)


class _StaticClassifier:
    def classify_intent(self, _query, _context):
        return SimpleNamespace(
            intent=QueryType.SEMANTIC_SEARCH,
            confidence=0.1,
            metadata={},
        )


class _MemoryHarness:
    def __init__(self, *, failed: bool = False, fail_writes: bool = False):
        self.failed = failed
        self.fail_writes = fail_writes
        self.context = None
        self.intent = (None, None)

    def get_context(self):
        return self.context

    def get_intent(self):
        return self.intent

    def set_context(self, context):
        if self.fail_writes:
            raise RuntimeError("private session detail")
        self.context = {"query": context.query, "building": context.building}

    def set_intent(self, intent, confidence):
        if self.fail_writes:
            raise RuntimeError("private intent detail")
        value = intent.value if hasattr(intent, "value") else intent
        self.intent = (value, confidence)

    def set_failed(self, failed):
        self.failed = bool(failed)


def _stub_pipeline(monkeypatch, memory: _MemoryHarness) -> QueryManager:
    manager = QueryManager(intent_classifier=_StaticClassifier())
    monkeypatch.setattr(
        "query_core.query_manager.missing_required_query_dependency", lambda: None
    )
    monkeypatch.setattr(manager, "_run_preprocessors", lambda _context: [])
    monkeypatch.setattr(
        manager, "_record_building_directory_readiness", lambda _context: False
    )
    monkeypatch.setattr(SessionManager, "get_last_query_context", memory.get_context)
    monkeypatch.setattr(SessionManager, "get_last_intent", memory.get_intent)
    monkeypatch.setattr(SessionManager, "set_last_query_context", memory.set_context)
    monkeypatch.setattr(SessionManager, "set_last_intent", memory.set_intent)
    monkeypatch.setattr(
        SessionManager,
        "conversation_memory_persistence_failed",
        lambda: memory.failed,
    )
    monkeypatch.setattr(
        SessionManager,
        "set_conversation_memory_persistence_failed",
        memory.set_failed,
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


def test_persistence_failure_marks_turn_without_requesting_current_notice(monkeypatch):
    memory = _MemoryHarness(fail_writes=True)
    manager = _stub_pipeline(monkeypatch, memory)

    result = _process(manager, "Tell me about unusual ventilation details")

    assert result.status is OutcomeStatus.DEGRADED
    assert result.degraded_components == [COMPONENT_CONVERSATION_MEMORY]
    assert result.metadata["conversation_memory_degradation"] == {
        "persistence_failed": True,
        "previous_turn_unavailable": False,
        "user_notice_required": False,
    }
    assert query_degradation_notice_required(result) is False
    assert "private session detail" not in str(result.to_dict())
    assert memory.failed is True
    assert manager.cache == {}
    assert (
        get_telemetry().get(
            METRIC_FALLBACK_ACTIVATED,
            component=COMPONENT_CONVERSATION_MEMORY,
        )
        >= 1
    )


def test_later_followup_warns_when_previous_context_was_not_saved(monkeypatch):
    memory = _MemoryHarness(failed=True)
    manager = _stub_pipeline(monkeypatch, memory)

    result = _process(manager, "What about that one?")

    assert result.status is OutcomeStatus.DEGRADED
    assert result.metadata["conversation_memory_degradation"] == {
        "persistence_failed": False,
        "previous_turn_unavailable": True,
        "user_notice_required": True,
    }
    assert query_degradation_notice_required(result) is True
    presented = present_query_failure(result)
    assert presented.message == "I couldn't use context from your previous question."
    assert "restate" in presented.action.lower()
    assert memory.failed is False
    assert manager.cache == {}


def test_standalone_query_clears_marker_without_a_user_warning(monkeypatch):
    memory = _MemoryHarness(failed=True)
    manager = _stub_pipeline(monkeypatch, memory)

    result = _process(manager, "Tell me about unusual ventilation details")

    assert result.status is OutcomeStatus.SUCCESS
    assert result.degraded_components == []
    assert "conversation_memory_degradation" not in result.metadata
    assert query_degradation_notice_required(result) is False
    assert memory.failed is False


def test_memory_read_failure_is_safe_and_material_only_for_followup(monkeypatch):
    memory = _MemoryHarness()
    manager = _stub_pipeline(monkeypatch, memory)
    monkeypatch.setattr(
        SessionManager,
        "conversation_memory_persistence_failed",
        lambda: (_ for _ in ()).throw(RuntimeError("private read detail")),
    )

    result = _process(manager, "And what about that one?")

    assert result.status is OutcomeStatus.DEGRADED
    assert (
        result.metadata["conversation_memory_degradation"]["previous_turn_unavailable"]
        is True
    )
    assert (
        result.metadata["conversation_memory_degradation"]["user_notice_required"]
        is True
    )
    assert "private read detail" not in str(result.to_dict())


def test_cached_result_is_degraded_when_memory_persistence_fails(monkeypatch):
    memory = _MemoryHarness()
    manager = _stub_pipeline(monkeypatch, memory)
    query = "Tell me about unusual ventilation details"
    first = _process(manager, query)
    assert first.status is OutcomeStatus.SUCCESS
    assert manager.cache

    memory.fail_writes = True
    result = _process(manager, query)

    assert manager.stats["cached_queries"] == 1
    assert result.status is OutcomeStatus.DEGRADED
    assert result.degraded_components == [COMPONENT_CONVERSATION_MEMORY]
    assert (
        result.metadata["conversation_memory_degradation"]["persistence_failed"] is True
    )


def test_conversation_memory_degradation_never_upgrades_failed_result():
    result = QueryResult(
        query="follow up",
        answer=None,
        status=OutcomeStatus.FAILED,
    )

    QueryManager._apply_conversation_memory_degradation(
        result,
        previous_turn_unavailable=True,
        user_notice_required=True,
    )

    assert result.status is OutcomeStatus.FAILED
    assert result.degraded_components == [COMPONENT_CONVERSATION_MEMORY]
