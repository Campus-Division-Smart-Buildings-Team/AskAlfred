"""Shared structured outcomes for queries, ingestion, and service operations."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.failure_codes import FailureCode, get_failure_code_spec


class OutcomeStatus(str, Enum):
    """The complete set of terminal operation states."""

    SUCCESS = "success"
    EMPTY = "empty"
    LOW_CONFIDENCE = "low_confidence"
    REJECTED = "rejected"
    DEGRADED = "degraded"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CRITICAL_INCONSISTENT = "critical_inconsistent"


# Statuses that count as a usable (possibly-degraded) success for stats and
# routing. Rejected requests and system failures are excluded.
COMPATIBLE_SUCCESS_STATUSES = frozenset(
    {
        OutcomeStatus.SUCCESS,
        OutcomeStatus.EMPTY,
        OutcomeStatus.LOW_CONFIDENCE,
        OutcomeStatus.DEGRADED,
        OutcomeStatus.PARTIAL,
    }
)


def is_successful(status: OutcomeStatus | str) -> bool:
    """Return whether ``status`` counts as a usable (possibly-degraded) success.

    This replaces the removed ``QueryResult.success`` boolean: callers that need
    a coarse success signal should derive it from the structured status here so
    there is a single, authoritative definition.
    """

    resolved = status if isinstance(status, OutcomeStatus) else OutcomeStatus(status)
    return resolved in COMPATIBLE_SUCCESS_STATUSES


def new_correlation_id() -> str:
    """Create a short, opaque support reference unrelated to user/session data."""

    return f"alf-{secrets.token_hex(6)}"


@dataclass
class FailureInfo:
    """Safe failure details suitable for result objects and UI presentation."""

    code: FailureCode
    component: str
    retryable: bool
    correlation_id: str = field(default_factory=new_correlation_id)
    safe_context: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.code, FailureCode):
            self.code = FailureCode(self.code)
        if not self.component or not self.component.strip():
            raise ValueError("FailureInfo.component must not be empty")
        if not self.correlation_id or not self.correlation_id.strip():
            raise ValueError("FailureInfo.correlation_id must not be empty")
        self.safe_context = dict(self.safe_context)

    @classmethod
    def from_code(
        cls,
        code: FailureCode | str,
        component: str,
        *,
        correlation_id: str | None = None,
        safe_context: dict[str, object] | None = None,
    ) -> "FailureInfo":
        """Build failure information using registered retryability."""

        stable_code = code if isinstance(code, FailureCode) else FailureCode(code)
        spec = get_failure_code_spec(stable_code)
        kwargs: dict[str, Any] = {
            "code": stable_code,
            "component": component,
            "retryable": spec.retryable,
            "safe_context": safe_context or {},
        }
        if correlation_id is not None:
            kwargs["correlation_id"] = correlation_id
        return cls(**kwargs)

    def to_dict(self) -> dict[str, object]:
        """Return a transport-safe representation."""

        return {
            "code": self.code.value,
            "component": self.component,
            "retryable": self.retryable,
            "correlation_id": self.correlation_id,
            "safe_context": dict(self.safe_context),
        }


@dataclass
class SourceOutcome:
    """Health and result count for one retrieval or processing source."""

    source: str
    status: OutcomeStatus
    result_count: int = 0
    failure: FailureInfo | None = None

    def __post_init__(self) -> None:
        if not self.source or not self.source.strip():
            raise ValueError("SourceOutcome.source must not be empty")
        if not isinstance(self.status, OutcomeStatus):
            self.status = OutcomeStatus(self.status)
        if self.result_count < 0:
            raise ValueError("SourceOutcome.result_count must be non-negative")

    def to_dict(self) -> dict[str, object]:
        """Return a transport-safe representation."""

        return {
            "source": self.source,
            "status": self.status.value,
            "result_count": self.result_count,
            "failure": self.failure.to_dict() if self.failure else None,
        }


__all__ = [
    "COMPATIBLE_SUCCESS_STATUSES",
    "FailureInfo",
    "OutcomeStatus",
    "SourceOutcome",
    "is_successful",
    "new_correlation_id",
]
