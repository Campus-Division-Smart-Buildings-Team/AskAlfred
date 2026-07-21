"""Structured-outcome tests for the fallback semantic-search handler."""

import pytest

from core.failure_codes import FailureCode
from core.outcomes import OutcomeStatus
from query_core.query_context import QueryContext
from query_handlers.semantic_search_handler import SemanticSearchHandler


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("abc", id="below-character-threshold"),
        pytest.param("abcd", id="below-word-threshold"),
    ],
)
def test_short_semantic_query_is_rejected_for_insufficient_detail(query):
    result = SemanticSearchHandler().handle(QueryContext(query=query))

    assert result.status is OutcomeStatus.REJECTED
    assert result.failure is not None
    assert result.failure.code is FailureCode.INPUT_INSUFFICIENT_DETAIL
    assert result.failure.component == "semantic_search"
    assert result.failure.retryable is False
    assert result.results == []
    assert result.metadata["short_query"] is True
