#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared translators from handler exceptions to structured :class:`QueryResult`.

Handlers use these so a structured-retrieval outage surfaces as ``unavailable``
(retryable) and an unexpected handler error surfaces as ``failed`` — both with a
stable failure code and correlation reference, and neither leaking exception
text into the result object (SEARCH-18, UI-01).
"""

from __future__ import annotations

from core.alfred_exceptions import StructuredSearchUnavailable
from core.failure_codes import FailureCode
from core.outcomes import FailureInfo, OutcomeStatus
from query_core.query_result import QueryResult

QUERY_HANDLER_COMPONENT = "query_handler"
STRUCTURED_RETRIEVAL_COMPONENT = "structured_retrieval"


def structured_unavailable_result(
    query: str,
    handler_name: str,
    query_type: str,
    exc: StructuredSearchUnavailable,
) -> QueryResult:
    """Build an ``unavailable`` result for a total structured-retrieval outage."""

    failure = getattr(exc, "failure", None) or FailureInfo.from_code(
        FailureCode.STRUCTURED_SEARCH_UNAVAILABLE,
        STRUCTURED_RETRIEVAL_COMPONENT,
    )
    return QueryResult(
        query=query,
        answer=None,
        results=[],
        handler_used=handler_name,
        query_type=query_type,
        status=OutcomeStatus.UNAVAILABLE,
        failure=failure,
        metadata={"error": failure.code.value},
    )


def handler_failed_result(
    query: str,
    handler_name: str,
    query_type: str,
    *,
    error_code: str,
) -> QueryResult:
    """Build a ``failed`` result for an unexpected handler error."""

    failure = FailureInfo.from_code(
        FailureCode.HANDLER_EXECUTION_FAILED,
        QUERY_HANDLER_COMPONENT,
    )
    return QueryResult(
        query=query,
        answer=None,
        results=[],
        handler_used=handler_name,
        query_type=query_type,
        status=OutcomeStatus.FAILED,
        failure=failure,
        metadata={"error": error_code},
    )


__all__ = [
    "handler_failed_result",
    "structured_unavailable_result",
]
