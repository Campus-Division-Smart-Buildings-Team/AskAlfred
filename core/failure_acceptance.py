"""Executable Phase 0 acceptance inventory for every P0/P1 register state.

The register in ``plan/failure_and_degraded_states_plan.md`` is the narrative
source of truth. This module gives every high-priority row a stable outcome
contract and a deterministic pytest owner. Keeping the inventory executable
means a new P0/P1 row cannot be added without choosing a low-cardinality code,
terminal status, component owner, and characterization-test node.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.failure_codes import FailureCode
from core.outcomes import OutcomeStatus


@dataclass(frozen=True)
class FailureAcceptance:
    """Stable acceptance contract for one high-priority register row."""

    priority: str
    status: OutcomeStatus
    code: FailureCode
    component: str
    owning_test: str = ""


def _state(
    priority: str,
    status: OutcomeStatus,
    code: FailureCode,
    component: str,
) -> FailureAcceptance:
    return FailureAcceptance(priority, status, code, component)


P0_P1_FAILURE_ACCEPTANCE: dict[str, FailureAcceptance] = {
    "START-01": _state(
        "P1", OutcomeStatus.UNAVAILABLE, FailureCode.STARTUP_ARCHIVE_INVALID, "startup"
    ),
    "START-04": _state(
        "P1",
        OutcomeStatus.DEGRADED,
        FailureCode.BUILDING_DIRECTORY_UNAVAILABLE,
        "building_directory",
    ),
    "START-06": _state(
        "P0",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.RATE_LIMIT_BACKEND_UNAVAILABLE,
        "distributed_coordination",
    ),
    "START-09": _state(
        "P1",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.CONFIGURATION_INVALID,
        "service_readiness",
    ),
    "AUTH-02": _state(
        "P1",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.AUTH_CONFIGURATION_INVALID,
        "authentication",
    ),
    "AUTH-05": _state(
        "P1",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.AUTH_PROVIDER_UNAVAILABLE,
        "authentication",
    ),
    "AUTH-06": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.AUTH_PROVIDER_RESPONSE_INVALID,
        "authentication",
    ),
    "AUTH-07": _state(
        "P0",
        OutcomeStatus.FAILED,
        FailureCode.AUTH_CLAIMS_INVALID,
        "authentication",
    ),
    "AUTH-08": _state(
        "P0",
        OutcomeStatus.REJECTED,
        FailureCode.ACCESS_CONTEXT_INVALID,
        "access_control",
    ),
    "AUTH-09": _state(
        "P0",
        OutcomeStatus.REJECTED,
        FailureCode.ACCESS_ROLE_CONTEXT_INVALID,
        "access_control",
    ),
    "AUTH-10": _state(
        "P0",
        OutcomeStatus.REJECTED,
        FailureCode.ACCESS_ACL_METADATA_INVALID,
        "access_control",
    ),
    "AUTH-11": _state(
        "P1",
        OutcomeStatus.EMPTY,
        FailureCode.ACCESS_AUTHORIZED_SCOPE_EMPTY,
        "access_control",
    ),
    "AUTH-13": _state(
        "P0",
        OutcomeStatus.REJECTED,
        FailureCode.ACCESS_CONTEXT_INVALID,
        "access_control",
    ),
    "ROUTE-08": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.ROUTING_GRAPH_INVALID,
        "query_routing",
    ),
    "SEARCH-01": _state(
        "P0",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.SEARCH_INDEX_UNAVAILABLE,
        "retrieval",
    ),
    "SEARCH-02": _state(
        "P0",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.SEARCH_EMBEDDING_UNAVAILABLE,
        "retrieval",
    ),
    "SEARCH-03": _state(
        "P0",
        OutcomeStatus.PARTIAL,
        FailureCode.SEARCH_NAMESPACE_UNAVAILABLE,
        "retrieval",
    ),
    "SEARCH-04": _state(
        "P0",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.SEARCH_BACKEND_UNAVAILABLE,
        "retrieval",
    ),
    "SEARCH-05": _state(
        "P1",
        OutcomeStatus.PARTIAL,
        FailureCode.SEARCH_SOURCE_PARTIAL,
        "retrieval",
    ),
    "SEARCH-06": _state(
        "P1",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.STRUCTURED_SEARCH_UNAVAILABLE,
        "structured_retrieval",
    ),
    "SEARCH-12": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.SEARCH_CONTRACT_INVALID,
        "query_routing",
    ),
    "SEARCH-13": _state(
        "P1",
        OutcomeStatus.PARTIAL,
        FailureCode.ANSWER_GENERATION_UNAVAILABLE,
        "answer_generation",
    ),
    "SEARCH-18": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.HANDLER_EXECUTION_FAILED,
        "query_handler",
    ),
    "UI-01": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.HANDLER_EXECUTION_FAILED,
        "outcome_presenter",
    ),
    "INGEST-01": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.INGEST_CONFIGURATION_INVALID,
        "ingestion",
    ),
    "INGEST-02": _state(
        "P1",
        OutcomeStatus.REJECTED,
        FailureCode.INGEST_CONFIGURATION_INVALID,
        "ingestion",
    ),
    "INGEST-03": _state(
        "P1",
        OutcomeStatus.REJECTED,
        FailureCode.INGEST_INPUT_INVALID,
        "ingestion",
    ),
    "INGEST-04": _state(
        "P0",
        OutcomeStatus.REJECTED,
        FailureCode.INGEST_INPUT_INVALID,
        "file_validation",
    ),
    "INGEST-05": _state(
        "P1",
        OutcomeStatus.REJECTED,
        FailureCode.INGEST_INPUT_INVALID,
        "file_validation",
    ),
    "INGEST-07": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.INGEST_FILE_UNREADABLE,
        "document_processing",
    ),
    "INGEST-09": _state(
        "P1",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.INGEST_FILE_TIMEOUT,
        "document_processing",
    ),
    "INGEST-10": _state(
        "P1",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.INGEST_RESOURCE_EXHAUSTED,
        "ingestion",
    ),
    "VECTOR-01": _state(
        "P1",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.INGEST_EMBEDDING_FAILED,
        "embedding",
    ),
    "VECTOR-02": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.INGEST_CONFIGURATION_INVALID,
        "embedding",
    ),
    "VECTOR-03": _state(
        "P1",
        OutcomeStatus.PARTIAL,
        FailureCode.INGEST_EMBEDDING_PARTIAL,
        "embedding",
    ),
    "VECTOR-04": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.INGEST_EMBEDDING_RESPONSE_INVALID,
        "embedding",
    ),
    "VECTOR-05": _state(
        "P1",
        OutcomeStatus.PARTIAL,
        FailureCode.INGEST_EMBEDDING_PARTIAL,
        "embedding",
    ),
    "VECTOR-06": _state(
        "P1",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.VECTOR_UPSERT_FAILED,
        "vector_write",
    ),
    "VECTOR-07": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.VECTOR_UPSERT_FAILED,
        "vector_write",
    ),
    "VECTOR-08": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.INGEST_WORKER_STALE,
        "vector_write",
    ),
    "VECTOR-09": _state(
        "P1",
        OutcomeStatus.PARTIAL,
        FailureCode.INGEST_WORKER_TIMEOUT,
        "vector_write",
    ),
    "VECTOR-10": _state(
        "P1",
        OutcomeStatus.REJECTED,
        FailureCode.VECTOR_UPSERT_CANCELLED,
        "vector_write",
    ),
    "VECTOR-11": _state(
        "P0",
        OutcomeStatus.FAILED,
        FailureCode.VECTOR_VERIFICATION_FAILED,
        "vector_verification",
    ),
    "VECTOR-12": _state(
        "P1",
        OutcomeStatus.PARTIAL,
        FailureCode.REGISTRY_DIVERGED,
        "ingest_registry",
    ),
    "VECTOR-13": _state(
        "P0",
        OutcomeStatus.REJECTED,
        FailureCode.INGEST_WORKER_STALE,
        "ingest_registry",
    ),
    "VECTOR-16": _state(
        "P0",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.VECTOR_VERIFICATION_UNAVAILABLE,
        "vector_verification",
    ),
    "FRA-03": _state(
        "P1",
        OutcomeStatus.REJECTED,
        FailureCode.FRA_ALREADY_PROCESSED,
        "fra_transaction",
    ),
    "FRA-04": _state(
        "P0",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.FRA_LOCK_UNAVAILABLE,
        "fra_transaction",
    ),
    "FRA-05": _state(
        "P1",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.FRA_SUPERSESSION_FAILED,
        "fra_transaction",
    ),
    "FRA-06": _state(
        "P0",
        OutcomeStatus.PARTIAL,
        FailureCode.FRA_SUPERSESSION_FAILED,
        "fra_transaction",
    ),
    "FRA-07": _state(
        "P0",
        OutcomeStatus.FAILED,
        FailureCode.FRA_SUPERSESSION_FAILED,
        "fra_transaction",
    ),
    "FRA-08": _state(
        "P0",
        OutcomeStatus.CRITICAL_INCONSISTENT,
        FailureCode.FRA_ROLLBACK_FAILED,
        "fra_transaction",
    ),
    "FRA-09": _state(
        "P1",
        OutcomeStatus.PARTIAL,
        FailureCode.REGISTRY_DIVERGED,
        "fra_transaction",
    ),
    "FRA-10": _state(
        "P1",
        OutcomeStatus.DEGRADED,
        FailureCode.FRA_VERIFICATION_DELAYED,
        "fra_transaction",
    ),
    "FRA-11": _state(
        "P0",
        OutcomeStatus.CRITICAL_INCONSISTENT,
        FailureCode.FRA_CRITICAL_INCONSISTENT,
        "fra_transaction",
    ),
    "FRA-12": _state(
        "P0",
        OutcomeStatus.CRITICAL_INCONSISTENT,
        FailureCode.FRA_JOURNAL_UNAVAILABLE,
        "fra_transaction",
    ),
    "FRA-13": _state(
        "P1",
        OutcomeStatus.UNAVAILABLE,
        FailureCode.FRA_IDEMPOTENCY_UNAVAILABLE,
        "fra_transaction",
    ),
    "RUN-03": _state(
        "P1",
        OutcomeStatus.PARTIAL,
        FailureCode.INGEST_RUN_PARTIAL,
        "ingest_run",
    ),
    "RUN-04": _state(
        "P1",
        OutcomeStatus.PARTIAL,
        FailureCode.INGEST_RUN_PARTIAL,
        "ingest_run",
    ),
    "RUN-05": _state(
        "P1",
        OutcomeStatus.FAILED,
        FailureCode.INGEST_RUN_FAILED,
        "ingest_run",
    ),
    "RUN-08": _state(
        "P0",
        OutcomeStatus.CRITICAL_INCONSISTENT,
        FailureCode.FRA_CRITICAL_INCONSISTENT,
        "ingest_run",
    ),
}

P0_P1_FAILURE_ACCEPTANCE = {
    state_id: replace(
        contract,
        owning_test=(
            "tests/test_failure_acceptance_inventory.py::"
            f"test_p0_p1_failure_behaviour[{state_id}]"
        ),
    )
    for state_id, contract in P0_P1_FAILURE_ACCEPTANCE.items()
}


__all__ = ["FailureAcceptance", "P0_P1_FAILURE_ACCEPTANCE"]
