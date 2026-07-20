"""Contract tests for structured operation outcomes."""

import re

import pytest

from config import TARGET_INDEXES
from config.source_classification import (
    RETRIEVAL_SOURCE_CLASSIFICATIONS,
    SourceRequirement,
    validate_target_index_classification,
)
from core.failure_codes import (
    FAILURE_CODE_SPECS,
    FailureCode,
    get_failure_code_spec,
)
from core.outcomes import FailureInfo, OutcomeStatus, SourceOutcome
from query_core.query_result import QueryResult


def test_outcome_status_contains_the_complete_taxonomy():
    assert {status.value for status in OutcomeStatus} == {
        "success",
        "empty",
        "low_confidence",
        "rejected",
        "degraded",
        "partial",
        "unavailable",
        "failed",
        "critical_inconsistent",
    }


def test_every_failure_code_has_registered_retryability():
    assert set(FAILURE_CODE_SPECS) == set(FailureCode)
    assert all(
        isinstance(spec.retryable, bool) for spec in FAILURE_CODE_SPECS.values()
    )


def test_unknown_failure_code_is_rejected():
    with pytest.raises(ValueError, match="Unknown failure code"):
        get_failure_code_spec("search.typo")


def test_every_target_index_has_an_explicit_source_classification():
    validate_target_index_classification(TARGET_INDEXES)

    assert set(RETRIEVAL_SOURCE_CLASSIFICATIONS) == set(TARGET_INDEXES)
    assert all(
        source.requirement in SourceRequirement
        for source in RETRIEVAL_SOURCE_CLASSIFICATIONS.values()
    )


def test_failure_info_uses_registered_retryability_and_opaque_reference():
    first = FailureInfo.from_code(
        FailureCode.SEARCH_BACKEND_UNAVAILABLE,
        "semantic_search",
        safe_context={"stage": "retrieval"},
    )
    second = FailureInfo.from_code(
        FailureCode.SEARCH_BACKEND_UNAVAILABLE,
        "semantic_search",
    )

    assert first.retryable is True
    assert first.correlation_id != second.correlation_id
    assert re.fullmatch(r"alf-[0-9a-f]{12}", first.correlation_id)
    assert first.to_dict() == {
        "code": "search.backend_unavailable",
        "component": "semantic_search",
        "retryable": True,
        "correlation_id": first.correlation_id,
        "safe_context": {"stage": "retrieval"},
    }


def test_source_outcome_validates_count_and_serialises_failure():
    failure = FailureInfo.from_code(
        FailureCode.SEARCH_NAMESPACE_UNAVAILABLE,
        "vector_store",
        correlation_id="alf-123456789abc",
    )
    outcome = SourceOutcome(
        source="property_documents",
        status=OutcomeStatus.UNAVAILABLE,
        failure=failure,
    )

    assert outcome.to_dict()["failure"] == failure.to_dict()
    with pytest.raises(ValueError, match="non-negative"):
        SourceOutcome(source="property_documents", status="success", result_count=-1)


def test_query_result_defaults_to_structured_success():
    result = QueryResult(query="hello", answer="Hello")

    assert result.status is OutcomeStatus.SUCCESS
    assert result.success is True


def test_legacy_false_maps_to_failed():
    result = QueryResult(query="hello", answer=None, success=False)

    assert result.status is OutcomeStatus.FAILED
    assert result.success is False


@pytest.mark.parametrize(
    ("status", "compatible_success"),
    [
        (OutcomeStatus.EMPTY, True),
        (OutcomeStatus.LOW_CONFIDENCE, True),
        (OutcomeStatus.DEGRADED, True),
        (OutcomeStatus.PARTIAL, True),
        (OutcomeStatus.REJECTED, False),
        (OutcomeStatus.UNAVAILABLE, False),
        (OutcomeStatus.FAILED, False),
        (OutcomeStatus.CRITICAL_INCONSISTENT, False),
    ],
)
def test_query_result_success_is_derived_from_status(status, compatible_success):
    result = QueryResult(query="hello", answer=None, status=status)

    assert result.success is compatible_success


def test_query_result_rejects_conflicting_legacy_and_structured_states():
    with pytest.raises(ValueError, match="conflicts"):
        QueryResult(
            query="hello",
            answer=None,
            success=True,
            status=OutcomeStatus.UNAVAILABLE,
        )


def test_query_result_serialises_new_contract_and_legacy_success():
    failure = FailureInfo.from_code(
        FailureCode.ANSWER_GENERATION_UNAVAILABLE,
        "answer_generator",
        correlation_id="alf-123456789abc",
    )
    source = SourceOutcome(
        source="property_documents",
        status=OutcomeStatus.SUCCESS,
        result_count=2,
    )
    result = QueryResult(
        query="hello",
        answer=None,
        results=[{"title": "Document"}],
        status=OutcomeStatus.PARTIAL,
        failure=failure,
        degraded_components=["answer_generator"],
        source_outcomes=[source],
    )

    payload = result.to_dict()

    assert payload["status"] == "partial"
    assert payload["success"] is True
    assert payload["failure"]["code"] == "answer.generation_unavailable"
    assert payload["source_outcomes"] == [
        {
            "source": "property_documents",
            "status": "success",
            "result_count": 2,
            "failure": None,
        }
    ]


def test_query_result_mutable_defaults_are_not_shared():
    first = QueryResult(query="one", answer="one")
    second = QueryResult(query="two", answer="two")

    first.results.append({"id": "one"})
    first.degraded_components.append("classifier")
    first.source_outcomes.append(
        SourceOutcome(source="documents", status=OutcomeStatus.EMPTY)
    )
    first.metadata["one"] = True

    assert second.results == []
    assert second.degraded_components == []
    assert second.source_outcomes == []
    assert second.metadata == {}
