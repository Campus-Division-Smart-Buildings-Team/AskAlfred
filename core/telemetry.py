#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Low-cardinality service telemetry and component readiness (plan section H).

This module is the query/service-side counterpart of the ingestion metrics
exporter. It provides two things:

* A :class:`Telemetry` recorder for low-cardinality counters keyed by a stable
  metric name plus a small set of enum-derived labels. It records request
  outcomes, per-source outcomes, fallback activations, degraded-service events
  (e.g. a rate-limit backend outage), and ACL-metadata drops.
* A :class:`ReadinessRegistry` that publishes whether each named component is
  ``ready``, ``degraded``, or ``unavailable`` so an operator view or health
  probe can report component readiness without inspecting logs.

Label safety is enforced, not merely documented: every label value must be a
short, low-cardinality token (or an :class:`~enum.Enum` value). Exception text,
user IDs, queries, document names, and file paths are rejected because they
would explode metric cardinality and leak sensitive data (plan section H).
"""

from __future__ import annotations

import re
import threading
from enum import Enum
from typing import Optional

from core.failure_codes import FailureCode
from core.ingest_outcomes import IngestTerminalStatus
from core.outcomes import OutcomeStatus

# ---------------------------------------------------------------------------
# Stable component names (low-cardinality label values / readiness keys)
# ---------------------------------------------------------------------------

COMPONENT_RATE_LIMITER = "rate_limiter"
COMPONENT_RESOURCE_LEASE = "resource_lease"
COMPONENT_INTENT_CLASSIFIER = "intent_classifier"
COMPONENT_BUILDING_DIRECTORY = "building_directory"
COMPONENT_RETRIEVAL = "retrieval"
COMPONENT_ANSWER_GENERATION = "answer_generation"
COMPONENT_ACCESS_CONTROL = "access_control"

# Stable metric names.
METRIC_REQUEST_OUTCOME = "request_outcome_total"
METRIC_SOURCE_OUTCOME = "source_outcome_total"
METRIC_FALLBACK_ACTIVATED = "fallback_activated_total"
METRIC_SERVICE_DEGRADED = "service_degraded_total"
METRIC_ACL_METADATA_DROP = "acl_metadata_drop_total"
METRIC_INGEST_OUTCOME = "ingest_outcome_total"
METRIC_INGEST_INTEGRITY = "ingest_integrity_total"

# A label value must be a short, low-cardinality token. Enum values are coerced
# to their ``.value`` first; anything else must match this pattern.
_LABEL_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_LABEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_METRIC_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class Readiness(str, Enum):
    """Coarse component health suitable for a readiness/health surface."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


def _coerce_label_value(value: object) -> str:
    """Return a validated low-cardinality label value or raise ``ValueError``.

    Enum members are reduced to their value. Every other value must be a short
    token matching :data:`_LABEL_VALUE_RE`; free text, exception strings, IDs,
    and paths are rejected so they can never become metric labels.
    """

    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        token = value.strip()
        if _LABEL_VALUE_RE.fullmatch(token):
            return token
        raise ValueError(f"Unsafe telemetry label value: {value!r}")
    raise ValueError(f"Unsupported telemetry label value type: {type(value)!r}")


def _label_key(labels: dict[str, object]) -> tuple[tuple[str, str], ...]:
    validated: list[tuple[str, str]] = []
    for name, value in labels.items():
        if not _LABEL_NAME_RE.fullmatch(name):
            raise ValueError(f"Unsafe telemetry label name: {name!r}")
        validated.append((name, _coerce_label_value(value)))
    return tuple(sorted(validated))


class Telemetry:
    """Thread-safe in-process counter store with label-cardinality guardrails."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}

    # -- generic primitive --------------------------------------------------

    def increment(self, metric: str, value: int = 1, **labels: object) -> None:
        """Increment ``metric`` by ``value`` for the given low-cardinality labels."""

        if not _METRIC_NAME_RE.fullmatch(metric):
            raise ValueError(f"Unsafe telemetry metric name: {metric!r}")
        key = (metric, _label_key(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + int(value)

    def get(self, metric: str, **labels: object) -> int:
        """Return the current counter value for ``metric``/labels (0 if unset)."""

        key = (metric, _label_key(labels))
        with self._lock:
            return self._counters.get(key, 0)

    def snapshot(self) -> dict[str, int]:
        """Return a flat ``"metric{label=value,...}" -> count`` mapping."""

        with self._lock:
            items = list(self._counters.items())
        result: dict[str, int] = {}
        for (metric, labels), count in items:
            if labels:
                rendered = ",".join(f"{name}={value}" for name, value in labels)
                result[f"{metric}{{{rendered}}}"] = count
            else:
                result[metric] = count
        return result

    def samples(self) -> list[tuple[str, tuple[tuple[str, str], ...], int]]:
        """Return ``(metric, sorted-labels, count)`` triples for exporters.

        Unlike :meth:`snapshot`, this keeps the label pairs structured so a
        metrics exporter can render them in a Prometheus exposition format
        without re-parsing a flattened key.
        """

        with self._lock:
            return [
                (metric, labels, count)
                for (metric, labels), count in self._counters.items()
            ]

    def reset(self) -> None:
        """Clear all counters (used by tests)."""

        with self._lock:
            self._counters.clear()

    # -- domain convenience -------------------------------------------------

    def record_request_outcome(
        self,
        status: OutcomeStatus,
        failure_code: Optional[FailureCode] = None,
    ) -> None:
        """Record one user-facing request keyed by terminal status and code."""

        labels: dict[str, object] = {"status": status}
        if failure_code is not None:
            labels["code"] = failure_code
        self.increment(METRIC_REQUEST_OUTCOME, **labels)

    def record_source_outcome(self, component: str, status: OutcomeStatus) -> None:
        """Record one per-source retrieval outcome for a named component."""

        self.increment(METRIC_SOURCE_OUTCOME, component=component, status=status)

    def record_fallback(self, component: str) -> None:
        """Record that a component fell back to a reduced-capability path."""

        self.increment(METRIC_FALLBACK_ACTIVATED, component=component)

    def record_service_degraded(
        self,
        component: str,
        failure_code: FailureCode,
    ) -> None:
        """Record a degraded-service event (e.g. a fail-open backend outage)."""

        self.increment(
            METRIC_SERVICE_DEGRADED, component=component, code=failure_code
        )

    def record_acl_metadata_drop(self, count: int = 1) -> None:
        """Record matches dropped for missing/invalid ACL metadata (AUTH-10)."""

        if count > 0:
            self.increment(METRIC_ACL_METADATA_DROP, value=count)

    def record_ingest_outcome(
        self, scope: str, status: IngestTerminalStatus | str
    ) -> None:
        """Record a file/run terminal state without file identifiers."""

        self.increment(METRIC_INGEST_OUTCOME, scope=scope, status=status)

    def record_ingest_integrity(self, event: str, state: str) -> None:
        """Record registry, rollback, or reconciliation state transitions."""

        self.increment(METRIC_INGEST_INTEGRITY, event=event, state=state)


class ReadinessRegistry:
    """Thread-safe registry of coarse component readiness states."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, tuple[Readiness, Optional[FailureCode]]] = {}

    def set(
        self,
        component: str,
        readiness: Readiness,
        failure_code: Optional[FailureCode] = None,
    ) -> None:
        with self._lock:
            self._states[component] = (readiness, failure_code)

    def mark_ready(self, component: str) -> None:
        self.set(component, Readiness.READY)

    def mark_degraded(
        self, component: str, failure_code: Optional[FailureCode] = None
    ) -> None:
        self.set(component, Readiness.DEGRADED, failure_code)

    def mark_unavailable(
        self, component: str, failure_code: Optional[FailureCode] = None
    ) -> None:
        self.set(component, Readiness.UNAVAILABLE, failure_code)

    def get(self, component: str) -> Readiness:
        """Return the recorded readiness (``READY`` when never recorded)."""

        with self._lock:
            state = self._states.get(component)
        return state[0] if state else Readiness.READY

    def is_healthy(self, component: str) -> bool:
        return self.get(component) is Readiness.READY

    def snapshot(self) -> dict[str, dict[str, Optional[str]]]:
        """Return a transport-safe ``component -> {readiness, code}`` mapping."""

        with self._lock:
            items = list(self._states.items())
        return {
            component: {
                "readiness": readiness.value,
                "code": code.value if code is not None else None,
            }
            for component, (readiness, code) in items
        }

    def reset(self) -> None:
        """Clear all readiness state (used by tests)."""

        with self._lock:
            self._states.clear()


# ---------------------------------------------------------------------------
# Process-wide singletons
# ---------------------------------------------------------------------------

_telemetry = Telemetry()
_readiness = ReadinessRegistry()


def get_telemetry() -> Telemetry:
    """Return the process-wide telemetry recorder."""

    return _telemetry


def get_readiness() -> ReadinessRegistry:
    """Return the process-wide component readiness registry."""

    return _readiness


__all__ = [
    "COMPONENT_ACCESS_CONTROL",
    "COMPONENT_ANSWER_GENERATION",
    "COMPONENT_BUILDING_DIRECTORY",
    "COMPONENT_INTENT_CLASSIFIER",
    "COMPONENT_RATE_LIMITER",
    "COMPONENT_RESOURCE_LEASE",
    "COMPONENT_RETRIEVAL",
    "METRIC_ACL_METADATA_DROP",
    "METRIC_FALLBACK_ACTIVATED",
    "METRIC_REQUEST_OUTCOME",
    "METRIC_INGEST_OUTCOME",
    "METRIC_INGEST_INTEGRITY",
    "METRIC_SERVICE_DEGRADED",
    "METRIC_SOURCE_OUTCOME",
    "Readiness",
    "ReadinessRegistry",
    "Telemetry",
    "get_readiness",
    "get_telemetry",
]
