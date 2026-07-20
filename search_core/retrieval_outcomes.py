#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Typed retrieval-source outcomes and aggregation (Phase 2, plan section B).

This module turns a set of per-source :class:`~core.outcomes.SourceOutcome`
records into a single aggregate :class:`~core.outcomes.OutcomeStatus` plus an
optional transport-safe :class:`~core.outcomes.FailureInfo`, following the
Section B aggregation rules:

1. Healthy sources with zero matches and no failures -> ``empty`` (the caller
   refines ``success``/``empty``/``low_confidence`` from result counts, so this
   module returns ``success`` for the no-failure case).
2. Some sources return results and any source fails -> ``partial``.
3. Some sources are healthy but empty and others fail -> ``partial`` (completeness
   unknown).
4. Every *required* source fails -> ``unavailable``/``failed`` by retryability.

A source is classified *required* or *optional* by
``config.source_classification``; unknown sources are treated as required so a
missing classification fails safe rather than silently degrading.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.source_classification import (
    RETRIEVAL_SOURCE_CLASSIFICATIONS,
    SourceRequirement,
)
from core.alfred_exceptions import StructuredSearchUnavailable
from core.failure_codes import FailureCode, get_failure_code_spec
from core.outcomes import FailureInfo, OutcomeStatus, SourceOutcome

RETRIEVAL_COMPONENT = "retrieval"
STRUCTURED_RETRIEVAL_COMPONENT = "structured_retrieval"

# Source statuses that mean the source produced no trustworthy, complete data.
_FULLY_FAILED_STATUSES = frozenset({OutcomeStatus.UNAVAILABLE, OutcomeStatus.FAILED})
_DEGRADED_STATUSES = frozenset(
    {
        OutcomeStatus.UNAVAILABLE,
        OutcomeStatus.FAILED,
        OutcomeStatus.PARTIAL,
        OutcomeStatus.DEGRADED,
    }
)

_NON_RETRYABLE_EMBEDDING_ERRORS = frozenset(
    {
        "AuthenticationError",
        "PermissionDeniedError",
        "BadRequestError",
        "NotFoundError",
        "ConfigError",
        "ConfigurationError",
    }
)


def embedding_failure_outcome(
    source: str,
    error: Exception,
) -> SourceOutcome:
    """Classify an embedding failure without retaining exception text."""

    non_retryable = type(error).__name__ in _NON_RETRYABLE_EMBEDDING_ERRORS
    code = (
        FailureCode.SEARCH_EMBEDDING_FAILED
        if non_retryable
        else FailureCode.SEARCH_EMBEDDING_UNAVAILABLE
    )
    return SourceOutcome(
        source=source,
        status=OutcomeStatus.FAILED if non_retryable else OutcomeStatus.UNAVAILABLE,
        failure=FailureInfo.from_code(
            code,
            RETRIEVAL_COMPONENT,
            safe_context={"phase": "embedding"},
        ),
    )


def source_requirement(source: str) -> SourceRequirement:
    """Return the required/optional classification for a retrieval source."""

    classification = RETRIEVAL_SOURCE_CLASSIFICATIONS.get(source)
    if classification is None:
        # Fail safe: an unclassified source is treated as required so its outage
        # cannot be masked as an optional degradation.
        return SourceRequirement.REQUIRED
    return classification.requirement


def source_fully_failed(outcome: SourceOutcome) -> bool:
    """True when the source returned no trustworthy data at all."""

    return outcome.status in _FULLY_FAILED_STATUSES


def source_degraded(outcome: SourceOutcome) -> bool:
    """True when the source failed or returned incomplete/partial data."""

    return outcome.failure is not None or outcome.status in _DEGRADED_STATUSES


def aggregate_source_outcomes(
    outcomes: list[SourceOutcome],
    *,
    component: str = RETRIEVAL_COMPONENT,
    backend_code: FailureCode = FailureCode.SEARCH_BACKEND_UNAVAILABLE,
    partial_code: FailureCode = FailureCode.SEARCH_SOURCE_PARTIAL,
) -> tuple[OutcomeStatus, FailureInfo | None]:
    """Aggregate per-source outcomes into a single retrieval status.

    Returns ``(status, failure)``. When every required source has fully failed,
    ``status`` is ``unavailable`` or ``failed`` (by the backend code's
    retryability) and ``failure`` is populated. When some sources fail while
    others remain usable, ``status`` is ``partial``. Otherwise ``status`` is
    ``success`` and the caller refines ``empty``/``low_confidence`` from the
    result count.
    """

    if not outcomes:
        return OutcomeStatus.SUCCESS, None

    required = [o for o in outcomes if source_requirement(o.source) is SourceRequirement.REQUIRED]
    # If nothing is marked required (all optional), treat the whole set as
    # critical so a total outage still fails rather than reporting partial.
    critical = required or list(outcomes)

    if all(source_fully_failed(o) for o in critical):
        # Preserve the specific per-source failure when a single source is at
        # fault; otherwise report a generic backend outage.
        if len(critical) == 1 and critical[0].failure is not None:
            failure = critical[0].failure
            status = (
                OutcomeStatus.UNAVAILABLE
                if failure.retryable
                else OutcomeStatus.FAILED
            )
            return status, failure

        spec = get_failure_code_spec(backend_code)
        status = OutcomeStatus.UNAVAILABLE if spec.retryable else OutcomeStatus.FAILED
        failure = FailureInfo.from_code(
            backend_code,
            component,
            safe_context={"failed_sources": len(critical)},
        )
        return status, failure

    if any(source_degraded(o) for o in outcomes):
        degraded_count = sum(1 for o in outcomes if source_degraded(o))
        failure = FailureInfo.from_code(
            partial_code,
            component,
            safe_context={"degraded_sources": degraded_count},
        )
        return OutcomeStatus.PARTIAL, failure

    return OutcomeStatus.SUCCESS, None


def raise_if_backend_unavailable(
    outcomes: list[SourceOutcome],
    *,
    component: str = STRUCTURED_RETRIEVAL_COMPONENT,
    backend_code: FailureCode = FailureCode.STRUCTURED_SEARCH_UNAVAILABLE,
) -> None:
    """Raise :class:`StructuredSearchUnavailable` when every required source failed.

    A total structured-retrieval outage must surface as ``unavailable`` rather
    than a misleading "no matching data" answer. Partial degradation is left to
    best-effort handling because structured answers are rendered as text.
    """

    status, failure = aggregate_source_outcomes(
        outcomes, component=component, backend_code=backend_code
    )
    if status in {OutcomeStatus.UNAVAILABLE, OutcomeStatus.FAILED} and failure:
        raise StructuredSearchUnavailable(failure)


@dataclass
class SemanticOutcome:
    """Structured result of a federated semantic search.

    ``status`` is the aggregate outcome across retrieval and answer generation.
    Retrieval and answer generation are separate stages: when retrieval succeeds
    but answer generation fails, ``status`` is ``partial``, ``results`` are
    retained, and ``answer`` is empty so the caller can show direct results.
    """

    results: list[dict]
    answer: str
    publication_info: str
    score_too_low: bool
    status: OutcomeStatus
    failure: FailureInfo | None = None
    source_outcomes: list[SourceOutcome] = field(default_factory=list)
    degraded_components: list[str] = field(default_factory=list)

    def as_legacy_tuple(self) -> tuple[list[dict], str, str, bool]:
        """Return the legacy ``(results, answer, publication_info, score_too_low)``.

        Callers on the old 4-tuple contract keep working; the structured status
        is dropped for them, but ``results`` are only populated for outcomes
        that should surface results, so a backend outage yields an empty list
        (never a false ``empty`` answer string).
        """

        return self.results, self.answer, self.publication_info, self.score_too_low


@dataclass
class StructuredAnswerOutcome:
    """Structured result for a vector-backed counting/domain answer.

    Structured query helpers still produce a formatted answer rather than a
    result-card list, but they must preserve the same per-source health contract
    as semantic retrieval.  A partial source outage therefore travels with the
    usable answer instead of being discarded once formatting completes.
    """

    answer: str | None
    status: OutcomeStatus
    failure: FailureInfo | None = None
    source_outcomes: list[SourceOutcome] = field(default_factory=list)
    degraded_components: list[str] = field(default_factory=list)


def structured_answer_outcome(
    answer: str | None,
    outcomes: list[SourceOutcome],
    *,
    empty: bool = False,
) -> StructuredAnswerOutcome:
    """Build a structured answer while retaining aggregate source health."""

    status, failure = aggregate_source_outcomes(
        outcomes,
        component=STRUCTURED_RETRIEVAL_COMPONENT,
        backend_code=FailureCode.STRUCTURED_SEARCH_UNAVAILABLE,
    )
    degraded_components = (
        [STRUCTURED_RETRIEVAL_COMPONENT]
        if status is OutcomeStatus.PARTIAL
        else []
    )
    if status is OutcomeStatus.SUCCESS and (empty or not answer):
        status = OutcomeStatus.EMPTY

    return StructuredAnswerOutcome(
        answer=answer,
        status=status,
        failure=failure,
        source_outcomes=list(outcomes),
        degraded_components=degraded_components,
    )


def unavailable_structured_answer(
    failure: FailureInfo,
    outcomes: list[SourceOutcome],
) -> StructuredAnswerOutcome:
    """Build an unavailable/failed structured answer after a total outage."""

    status = OutcomeStatus.UNAVAILABLE if failure.retryable else OutcomeStatus.FAILED
    return StructuredAnswerOutcome(
        answer=None,
        status=status,
        failure=failure,
        source_outcomes=list(outcomes),
        degraded_components=[STRUCTURED_RETRIEVAL_COMPONENT],
    )


__all__ = [
    "RETRIEVAL_COMPONENT",
    "STRUCTURED_RETRIEVAL_COMPONENT",
    "SemanticOutcome",
    "StructuredAnswerOutcome",
    "aggregate_source_outcomes",
    "embedding_failure_outcome",
    "raise_if_backend_unavailable",
    "source_degraded",
    "source_fully_failed",
    "source_requirement",
    "structured_answer_outcome",
    "unavailable_structured_answer",
]
