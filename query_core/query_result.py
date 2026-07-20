#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Unified structured result returned by all query handlers."""

from dataclasses import dataclass, field
from typing import Any, Optional

from core.outcomes import (
    COMPATIBLE_SUCCESS_STATUSES,
    FailureInfo,
    OutcomeStatus,
    SourceOutcome,
)


@dataclass(init=False)
class QueryResult:
    """Represent the final response and its structured operation outcome.

    ``success`` remains available as a temporary compatibility property while
    handlers migrate to ``status``. New code should use ``status``, ``failure``,
    ``degraded_components``, and ``source_outcomes`` directly.
    """

    query: str
    answer: Optional[str]
    results: list[Any] = field(default_factory=list)
    handler_used: Optional[str] = None
    query_type: Optional[str] = None
    status: OutcomeStatus = OutcomeStatus.SUCCESS
    failure: FailureInfo | None = None
    degraded_components: list[str] = field(default_factory=list)
    source_outcomes: list[SourceOutcome] = field(default_factory=list)
    processing_time_ms: Optional[float] = None
    publication_date_info: Any = None
    score_too_low: Optional[bool] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        query: str,
        answer: Optional[str],
        results: list[Any] | None = None,
        handler_used: Optional[str] = None,
        query_type: Optional[str] = None,
        success: bool | None = None,
        processing_time_ms: Optional[float] = None,
        publication_date_info: Any = None,
        score_too_low: Optional[bool] = None,
        metadata: dict[str, Any] | None = None,
        *,
        status: OutcomeStatus | str | None = None,
        failure: FailureInfo | None = None,
        degraded_components: list[str] | None = None,
        source_outcomes: list[SourceOutcome] | None = None,
    ) -> None:
        """Create a result while accepting the legacy ``success`` argument.

        Existing callers that pass ``success=False`` are mapped to ``failed``
        until they are migrated to a more precise outcome.
        """

        resolved_status = (
            OutcomeStatus(status) if status is not None else OutcomeStatus.SUCCESS
        )
        if status is None and success is False:
            resolved_status = OutcomeStatus.FAILED
        elif success is not None:
            compatible_success = resolved_status in COMPATIBLE_SUCCESS_STATUSES
            if success != compatible_success:
                raise ValueError("success conflicts with the structured status")

        self.query = query
        self.answer = answer
        self.results = list(results) if results is not None else []
        self.handler_used = handler_used
        self.query_type = query_type
        self.status = resolved_status
        self.failure = failure
        self.degraded_components = list(degraded_components or [])
        self.source_outcomes = list(source_outcomes or [])
        self.processing_time_ms = processing_time_ms
        self.publication_date_info = publication_date_info
        self.score_too_low = score_too_low
        self.metadata = dict(metadata or {})

    @property
    def success(self) -> bool:
        """Temporary boolean compatibility view of the structured status."""

        return self.status in COMPATIBLE_SUCCESS_STATUSES

    @success.setter
    def success(self, value: bool) -> None:
        """Support legacy mutation while callers migrate to ``status``."""

        self.status = OutcomeStatus.SUCCESS if value else OutcomeStatus.FAILED

    def add_metadata(self, key: str, value: Any) -> None:
        """Add a single metadata item."""

        self.metadata[key] = value

    def merge_metadata(self, data: dict[str, Any]) -> None:
        """Merge handler metadata without overwriting existing keys."""

        if data:
            for key, value in data.items():
                if key not in self.metadata:
                    self.metadata[key] = value

    def to_dict(self) -> dict[str, Any]:
        """Convert the result to a transport-safe dictionary."""

        return {
            "query": self.query,
            "answer": self.answer,
            "results": self.results,
            "handler_used": self.handler_used,
            "query_type": self.query_type,
            "status": self.status.value,
            "success": self.success,
            "failure": self.failure.to_dict() if self.failure else None,
            "degraded_components": list(self.degraded_components),
            "source_outcomes": [
                source_outcome.to_dict()
                for source_outcome in self.source_outcomes
            ],
            "processing_time_ms": self.processing_time_ms,
            "publication_date_info": self.publication_date_info,
            "score_too_low": self.score_too_low,
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:
        """Readable representation for diagnostics."""

        return (
            f"QueryResult(query={self.query!r}, "
            f"handler={self.handler_used!r}, "
            f"query_type={self.query_type!r}, status={self.status.value!r}, "
            f"success={self.success}, results={len(self.results)} items)"
        )
