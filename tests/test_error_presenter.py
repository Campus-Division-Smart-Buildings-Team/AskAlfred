"""Contract tests for the central error presenter."""

import re

import pytest

from core.failure_codes import FailureCode
from core.outcomes import FailureInfo, OutcomeStatus
from query_core.query_result import QueryResult
from ui.error_presenter import (
    PresentedOutcome,
    present_outcome,
    present_query_failure,
)

# Anything that would leak implementation, deployment, or security detail into
# user-facing copy. The presenter's messages and actions must never contain it.
FORBIDDEN_SUBSTRINGS = (
    "exception",
    "traceback",
    "tenant",
    "namespace",
    "index",
    "pinecone",
    "openai",
    "redis",
    "azure",
    "env",
    "config",
    "none",
    "null",
    "stack",
    "token",
    "__",
)

CORRELATION_PATTERN = re.compile(r"alf-[0-9a-f]{12}")


def test_every_outcome_status_maps_to_a_presentation():
    for status in OutcomeStatus:
        presented = present_outcome(status)
        assert isinstance(presented, PresentedOutcome)
        assert presented.severity in {"success", "info", "warning", "error"}
        # Every non-success status must produce a shown message.
        if status is OutcomeStatus.SUCCESS:
            assert presented.render_as_notice is False
        else:
            assert presented.render_as_notice is True
            assert presented.message


def test_presented_copy_never_leaks_detail():
    presentations = [present_outcome(status) for status in OutcomeStatus]
    presentations.extend(
        present_outcome(status, FailureInfo.from_code(code, "component"))
        for status in (OutcomeStatus.REJECTED, OutcomeStatus.UNAVAILABLE)
        for code in FailureCode
    )

    for presented in presentations:
        text = f"{presented.message} {presented.action}".lower()
        for fragment in FORBIDDEN_SUBSTRINGS:
            assert fragment not in text, (fragment, text)


def test_unavailable_and_failed_always_carry_a_support_reference():
    for status in (
        OutcomeStatus.UNAVAILABLE,
        OutcomeStatus.FAILED,
        OutcomeStatus.CRITICAL_INCONSISTENT,
    ):
        presented = present_outcome(status)
        assert presented.reference is not None
        assert CORRELATION_PATTERN.fullmatch(presented.reference)
        assert presented.retry_suggested is (status is not OutcomeStatus.CRITICAL_INCONSISTENT)


def test_empty_and_low_confidence_do_not_invent_a_reference():
    for status in (OutcomeStatus.EMPTY, OutcomeStatus.LOW_CONFIDENCE):
        assert present_outcome(status).reference is None


def test_failure_reference_is_preserved_from_the_failure_object():
    failure = FailureInfo.from_code(
        FailureCode.SEARCH_BACKEND_UNAVAILABLE,
        "semantic_search",
        correlation_id="alf-123456789abc",
    )
    presented = present_outcome(OutcomeStatus.UNAVAILABLE, failure)
    assert presented.reference == "alf-123456789abc"


def test_access_context_rejection_is_privacy_preserving():
    failure = FailureInfo.from_code(
        FailureCode.ACCESS_CONTEXT_INVALID,
        "access_control",
        correlation_id="alf-123456789abc",
    )
    presented = present_outcome(OutcomeStatus.REJECTED, failure)

    assert presented.severity == "error"
    assert presented.reference == "alf-123456789abc"
    assert presented.retry_suggested is False
    # Must not reveal that inaccessible documents exist.
    assert "access" in presented.message.lower()
    assert "no result" not in presented.message.lower()
    assert "document" not in presented.message.lower()


def test_present_query_failure_reads_result_status_and_failure():
    failure = FailureInfo.from_code(
        FailureCode.ANSWER_GENERATION_UNAVAILABLE, "answer_generator"
    )
    result = QueryResult(
        query="hello",
        answer=None,
        status=OutcomeStatus.UNAVAILABLE,
        failure=failure,
    )

    presented = present_query_failure(result)

    assert presented.severity == "error"
    assert presented.reference == failure.correlation_id


def test_failed_result_without_failure_still_gets_a_reference():
    result = QueryResult(query="hello", answer=None, status=OutcomeStatus.FAILED)

    presented = present_query_failure(result)

    assert result.status is OutcomeStatus.FAILED
    assert presented.reference is not None
    assert CORRELATION_PATTERN.fullmatch(presented.reference)


def test_unknown_status_string_is_rejected():
    with pytest.raises(ValueError):
        present_outcome("not_a_status")
