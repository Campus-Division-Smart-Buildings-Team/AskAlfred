"""
Tests for the Query Manager,
ensuring correct routing of queries to handlers and backward compatibility of results.
"""

import pytest

from core.failure_codes import FailureCode
from core.outcomes import OutcomeStatus, is_successful
from query_core import query_context
from query_core.query_manager import QueryManager, process_query_unified
from query_core.query_result import QueryResult
from query_core.query_types import QueryType
from query_handlers.semantic_search_handler import SemanticSearchHandler


@pytest.fixture(autouse=True)
def _dev_anonymous_posture(monkeypatch):
    """Run routing tests under the development anonymous-access posture.

    These tests submit anonymous queries to exercise routing. Mandatory-auth
    rejection of anonymous sessions (AUTH-13) is covered separately in
    tests/test_phase3_degraded_and_access.py and test_access_context_validation.py.
    """
    monkeypatch.setattr(query_context, "IS_PRODUCTION", False)
    monkeypatch.setattr(query_context, "REQUIRE_AUTH", False)
    monkeypatch.setattr(query_context, "ALLOW_ANONYMOUS_DEV", True)

# Test queries with expected routing
TEST_CASES = [
    # (query, expected_query_type)
    ("Hello Alfred", QueryType.CONVERSATIONAL),
    ("Who are you?", QueryType.CONVERSATIONAL),
    ("Thank you", QueryType.CONVERSATIONAL),
    ("Goodbye", QueryType.CONVERSATIONAL),
    ("How many buildings have FRAs?", QueryType.COUNTING),
    ("Count the buildings", QueryType.COUNTING),
    ("Number of buildings with BMS", QueryType.COUNTING),
    ("Show maintenance requests for Senate House", QueryType.MAINTENANCE),
    ("List all maintenance jobs", QueryType.MAINTENANCE),
    ("Electrical maintenance requests", QueryType.MAINTENANCE),
    ("Rank buildings by area", QueryType.RANKING),
    ("Top 10 largest buildings", QueryType.RANKING),
    ("Sort buildings by gross area", QueryType.RANKING),
    ("Which buildings are Condition A?", QueryType.PROPERTY_CONDITION),
    ("Show derelict buildings", QueryType.PROPERTY_CONDITION),
    ("What is the BMS configuration?", QueryType.SEMANTIC_SEARCH),
    ("Tell me about HVAC systems", QueryType.SEMANTIC_SEARCH),
]


@pytest.fixture
def stub_retrieval_handlers(monkeypatch):
    """Keep routing tests focused on routing, not live retrieval services."""

    monkeypatch.setattr(QueryManager, "_run_preprocessors", lambda self, context: None)

    def fake_handle(self, context):
        return QueryResult(
            query=context.query,
            answer=f"{self.query_type.value} response",
            handler_used=self.__class__.__name__,
            query_type=self.query_type.value,
        )

    for handler_path in (
        "query_handlers.counting_handler.CountingHandler",
        "query_handlers.maintenance_handler.MaintenanceHandler",
        "query_handlers.property_handler.PropertyHandler",
        "query_handlers.ranking_handler.RankingHandler",
        "query_handlers.semantic_search_handler.SemanticSearchHandler",
    ):
        monkeypatch.setattr(f"{handler_path}.handle", fake_handle)


class TestQueryManager:
    """Test the Query Manager."""

    def setup_method(self):
        """Setup before each test."""
        self.manager = QueryManager()

    @pytest.mark.parametrize("query,expected_type", TEST_CASES)
    def test_query_routing(self, query, expected_type, stub_retrieval_handlers):
        """Test that queries route to correct handlers."""

        result = self.manager.process_query(query)

        assert result.query_type == expected_type.value, (
            f"Query '{query}' routed to {result.query_type}, "
            f"expected {expected_type.value}"
        )
        assert is_successful(
            result.status
        ), f"Query failed: {result.metadata.get('error')}"
        assert (
            result.answer is not None and len(result.answer) > 0
        ), "Empty answer returned"

    def test_conversational_responses(self):
        """Test conversational handler returns appropriate responses."""
        greetings = ["hello", "hi", "hey Alfred"]

        for greeting in greetings:
            result = self.manager.process_query(greeting)
            assert result.answer is not None and "Alfred" in result.answer
            assert result.answer is not None and "help" in result.answer.lower()

    def test_error_handling(self):
        """Test error handling for edge cases."""
        # Empty query
        result = self.manager.process_query("")
        # Should still return a result (even if error)
        assert result is not None
        assert isinstance(result.answer, str)

    def test_statistics(self, stub_retrieval_handlers):
        """Test statistics tracking."""
        # Process some queries
        queries = ["hello", "how many buildings?", "rank buildings by area"]

        for query in queries:
            self.manager.process_query(query)

        # Check stats
        stats = self.manager.get_statistics()
        assert stats["total_queries"] == len(queries)
        assert len(stats["query_types"]) > 0
        assert isinstance(stats["avg_time_ms"], float)
        assert stats["avg_time_ms"] >= 0

    def test_cache_entries_do_not_share_mutable_state(self):
        """Stored and returned cache values are independent deep copies."""
        self.manager.cache_enabled = True
        original = QueryResult(
            query="hello",
            answer="hi",
            results=[{"items": [1]}],
            metadata={"nested": {"value": 1}},
        )

        self.manager._store_cached_result("cache-key", original)
        original.results[0]["items"].append(2)
        original.metadata["nested"]["value"] = 2

        first = self.manager._get_cached_result("cache-key")
        assert first is not None
        assert first.results == [{"items": [1]}]
        assert first.metadata == {"nested": {"value": 1}}

        first.results[0]["items"].append(3)
        first.metadata["nested"]["value"] = 3

        second = self.manager._get_cached_result("cache-key")
        assert second is not None
        assert second.results == [{"items": [1]}]
        assert second.metadata == {"nested": {"value": 1}}


class TestHandlerGraphValidation:
    """ROUTE-08 custom handler graphs fail with a typed outcome."""

    @pytest.mark.parametrize(
        "config",
        [
            {},
            {
                "ConversationalHandler": {"enabled": True},
                "SemanticSearchHandler": {"enabled": False},
            },
            {
                "UnknownHandler": {"enabled": True},
                "SemanticSearchHandler": {"enabled": True},
            },
        ],
        ids=["empty-handler-list", "disabled-semantic", "unknown-handler"],
    )
    def test_invalid_custom_graph_returns_typed_failure(self, config):
        manager = QueryManager(config=config, intent_classifier=object())

        result = manager.process_query("Tell me about HVAC systems")

        assert result.status is OutcomeStatus.FAILED
        assert result.failure is not None
        assert result.failure.code is FailureCode.ROUTING_GRAPH_INVALID
        assert result.failure.component == "query_routing"
        assert result.failure.retryable is False
        assert result.handler_used is None
        assert result.answer is None
        assert result.results == []
        assert result.metadata["route"] == "graph_invalid"

    def test_duplicate_terminal_fallbacks_return_typed_failure(self, monkeypatch):
        monkeypatch.setattr(
            QueryManager,
            "_load_handlers_from_config",
            lambda self, config: [SemanticSearchHandler(), SemanticSearchHandler()],
        )
        manager = QueryManager(
            config={"SemanticSearchHandler": {"enabled": True}},
            intent_classifier=object(),
        )

        result = manager.process_query("Tell me about HVAC systems")

        assert result.status is OutcomeStatus.FAILED
        assert result.failure is not None
        assert result.failure.code is FailureCode.ROUTING_GRAPH_INVALID


class TestBackwardCompatibility:
    """Test that results match old system format."""

    def test_result_format(self, stub_retrieval_handlers):
        """Test QueryResult has all expected fields."""

        query = "What is the BMS configuration?"
        results, answer, pub_date, score_low = process_query_unified(query)

        assert isinstance(results, list)
        assert isinstance(answer, str)
        assert isinstance(pub_date, str)
        assert isinstance(score_low, bool)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
