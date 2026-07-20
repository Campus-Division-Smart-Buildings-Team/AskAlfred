"""Stable, low-cardinality failure codes used by operation outcomes.

The values in :class:`FailureCode` are part of the telemetry and presentation
contract. They must not contain exception text, user input, file paths, source
names, or other high-cardinality values.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FailureCode(str, Enum):
    """Machine-readable failure categories shared across application layers."""

    UNKNOWN = "internal.unknown"
    CONFIGURATION_INVALID = "configuration.invalid"
    STARTUP_ARCHIVE_INVALID = "startup.archive_invalid"
    BUILDING_DIRECTORY_UNAVAILABLE = "building.directory_unavailable"
    RATE_LIMIT_BACKEND_UNAVAILABLE = "rate_limit.backend_unavailable"

    AUTH_CONFIGURATION_INVALID = "auth.configuration_invalid"
    AUTH_PROVIDER_UNAVAILABLE = "auth.provider_unavailable"
    AUTH_PROVIDER_RESPONSE_INVALID = "auth.provider_response_invalid"
    AUTH_CLAIMS_INVALID = "auth.claims_invalid"
    ACCESS_CONTEXT_INVALID = "access.context_invalid"
    ACCESS_ROLE_CONTEXT_INVALID = "access.role_context_invalid"
    ACCESS_ACL_METADATA_INVALID = "access.acl_metadata_invalid"
    ACCESS_AUTHORIZED_SCOPE_EMPTY = "access.authorized_scope_empty"

    ROUTING_GRAPH_INVALID = "routing.graph_invalid"
    HANDLER_EXECUTION_FAILED = "handler.execution_failed"

    SEARCH_INDEX_UNAVAILABLE = "search.index_unavailable"
    SEARCH_EMBEDDING_UNAVAILABLE = "search.embedding_unavailable"
    SEARCH_EMBEDDING_FAILED = "search.embedding_failed"
    SEARCH_NAMESPACE_UNAVAILABLE = "search.namespace_unavailable"
    SEARCH_BACKEND_UNAVAILABLE = "search.backend_unavailable"
    SEARCH_SOURCE_PARTIAL = "search.source_partial"
    STRUCTURED_SEARCH_UNAVAILABLE = "search.structured_unavailable"
    SEARCH_CONTRACT_INVALID = "search.contract_invalid"
    ANSWER_GENERATION_UNAVAILABLE = "answer.generation_unavailable"
    CITATION_GENERATION_UNAVAILABLE = "citation.generation_unavailable"

    INGEST_CONFIGURATION_INVALID = "ingest.configuration_invalid"
    INGEST_INPUT_INVALID = "ingest.input_invalid"
    INGEST_FILE_CHANGED = "ingest.file_changed"
    INGEST_FILE_UNREADABLE = "ingest.file_unreadable"
    INGEST_FILE_TIMEOUT = "ingest.file_timeout"
    INGEST_RESOURCE_EXHAUSTED = "ingest.resource_exhausted"
    INGEST_CONTENT_EMPTY = "ingest.content_empty"
    INGEST_PARSE_FAILED = "ingest.parse_failed"
    INGEST_EMBEDDING_PARTIAL = "ingest.embedding_partial"
    INGEST_EMBEDDING_FAILED = "ingest.embedding_failed"
    INGEST_EMBEDDING_RESPONSE_INVALID = "ingest.embedding_response_invalid"
    VECTOR_UPSERT_FAILED = "vector.upsert_failed"
    VECTOR_UPSERT_CANCELLED = "vector.upsert_cancelled"
    VECTOR_VERIFICATION_UNAVAILABLE = "vector.verification_unavailable"
    VECTOR_VERIFICATION_FAILED = "vector.verification_failed"
    REGISTRY_UNAVAILABLE = "registry.unavailable"
    REGISTRY_DIVERGED = "registry.diverged"
    INGEST_WORKER_TIMEOUT = "ingest.worker_timeout"
    INGEST_WORKER_STALE = "ingest.worker_stale"
    INGEST_RUN_PARTIAL = "ingest.run_partial"
    INGEST_RUN_FAILED = "ingest.run_failed"

    FRA_LOCK_UNAVAILABLE = "fra.lock_unavailable"
    FRA_JOURNAL_UNAVAILABLE = "fra.journal_unavailable"
    FRA_ALREADY_PROCESSED = "fra.already_processed"
    FRA_SUPERSESSION_FAILED = "fra.supersession_failed"
    FRA_VERIFICATION_DELAYED = "fra.verification_delayed"
    FRA_ROLLBACK_FAILED = "fra.rollback_failed"
    FRA_CRITICAL_INCONSISTENT = "fra.critical_inconsistent"
    FRA_IDEMPOTENCY_UNAVAILABLE = "fra.idempotency_unavailable"


@dataclass(frozen=True)
class FailureCodeSpec:
    """Static behavior attached to a stable failure code."""

    retryable: bool


_NON_RETRYABLE_CODES = {
    FailureCode.CONFIGURATION_INVALID,
    FailureCode.STARTUP_ARCHIVE_INVALID,
    FailureCode.AUTH_CONFIGURATION_INVALID,
    FailureCode.AUTH_PROVIDER_RESPONSE_INVALID,
    FailureCode.AUTH_CLAIMS_INVALID,
    FailureCode.ACCESS_CONTEXT_INVALID,
    FailureCode.ACCESS_ROLE_CONTEXT_INVALID,
    FailureCode.ACCESS_ACL_METADATA_INVALID,
    FailureCode.ACCESS_AUTHORIZED_SCOPE_EMPTY,
    FailureCode.ROUTING_GRAPH_INVALID,
    FailureCode.SEARCH_CONTRACT_INVALID,
    FailureCode.SEARCH_EMBEDDING_FAILED,
    FailureCode.INGEST_CONFIGURATION_INVALID,
    FailureCode.INGEST_INPUT_INVALID,
    FailureCode.INGEST_FILE_CHANGED,
    FailureCode.INGEST_CONTENT_EMPTY,
    FailureCode.INGEST_PARSE_FAILED,
    FailureCode.VECTOR_UPSERT_CANCELLED,
    FailureCode.VECTOR_VERIFICATION_FAILED,
    FailureCode.INGEST_WORKER_STALE,
    FailureCode.FRA_ALREADY_PROCESSED,
    FailureCode.FRA_CRITICAL_INCONSISTENT,
}


FAILURE_CODE_SPECS: dict[FailureCode, FailureCodeSpec] = {
    code: FailureCodeSpec(retryable=code not in _NON_RETRYABLE_CODES)
    for code in FailureCode
}


def get_failure_code_spec(code: FailureCode | str) -> FailureCodeSpec:
    """Return registered behavior, rejecting unstable or misspelled codes."""

    try:
        stable_code = code if isinstance(code, FailureCode) else FailureCode(code)
    except ValueError as exc:
        raise ValueError(f"Unknown failure code: {code!r}") from exc
    return FAILURE_CODE_SPECS[stable_code]


__all__ = [
    "FAILURE_CODE_SPECS",
    "FailureCode",
    "FailureCodeSpec",
    "get_failure_code_spec",
]
