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

import bisect
import math
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
COMPONENT_CONVERSATION_MEMORY = "conversation_memory"
COMPONENT_RETRIEVAL = "retrieval"
COMPONENT_ANSWER_GENERATION = "answer_generation"
COMPONENT_ACCESS_CONTROL = "access_control"
COMPONENT_OBSERVABILITY = "observability"

# External dependency components validated at startup (START-09 / START-10).
# These are the *dependencies* (credential/connection configuration) rather than
# the runtime capabilities above; a startup readiness check publishes their
# state before any query runs.
COMPONENT_OPENAI = "openai"
COMPONENT_PINECONE = "pinecone"
COMPONENT_REDIS = "redis"

# Stable metric names.
METRIC_REQUEST_OUTCOME = "request_outcome_total"
METRIC_AUTH_OUTCOME = "auth_outcome_total"
METRIC_SOURCE_OUTCOME = "source_outcome_total"
METRIC_FALLBACK_ACTIVATED = "fallback_activated_total"
METRIC_SERVICE_DEGRADED = "service_degraded_total"
METRIC_ACL_METADATA_DROP = "acl_metadata_drop_total"
METRIC_ACL_RECONCILIATION = "acl_reconciliation_total"
METRIC_INGEST_OUTCOME = "ingest_outcome_total"
METRIC_INGEST_REVIEW = "ingest_review_total"
METRIC_INGEST_INTEGRITY = "ingest_integrity_total"
METRIC_INGEST_STALE_WRITER = "ingest_stale_writer_total"

# End-to-end user request latency. Unlike the counters above this is a histogram
# metric (see :meth:`Telemetry.observe`).
METRIC_REQUEST_DURATION = "request_duration_seconds"

# Fixed histogram bucket upper bounds (seconds) for end-to-end request latency.
# The span deliberately covers cache-hit responses (tens of ms) through
# OpenAI-bound answer generation (tens of seconds). Bounds are a stable exposition
# contract: changing them re-buckets historical series, so treat an edit as a
# coordinated dashboard/alert migration rather than a routine tweak.
REQUEST_DURATION_BUCKETS: tuple[float, ...] = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    20.0,
    30.0,
    60.0,
)

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


class _Histogram:
    """Fixed-bound histogram accumulator for a single metric/label series."""

    __slots__ = ("bounds", "counts", "sum", "count")

    def __init__(self, bounds: tuple[float, ...]) -> None:
        self.bounds = bounds
        # One counter per finite bound plus a trailing +Inf overflow bucket.
        self.counts = [0] * (len(bounds) + 1)
        self.sum = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        # ``bisect_left`` maps ``value`` to the first bound ``b`` with
        # ``value <= b`` (Prometheus ``le`` semantics); a value above every bound
        # lands in the trailing overflow bucket at index ``len(bounds)``.
        self.counts[bisect.bisect_left(self.bounds, value)] += 1
        self.sum += value
        self.count += 1


class Telemetry:
    """Thread-safe in-process counter/histogram store with label guardrails."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._histograms: dict[
            tuple[str, tuple[tuple[str, str], ...]], _Histogram
        ] = {}
        # Bucket bounds are pinned to the first set seen per metric so the
        # exposed series never changes bucketing mid-process.
        self._histogram_bounds: dict[str, tuple[float, ...]] = {}

    # -- generic primitive --------------------------------------------------

    def increment(self, metric: str, value: int = 1, **labels: object) -> None:
        """Increment ``metric`` by ``value`` for the given low-cardinality labels."""

        if not _METRIC_NAME_RE.fullmatch(metric):
            raise ValueError(f"Unsafe telemetry metric name: {metric!r}")
        key = (metric, _label_key(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + int(value)

    def observe(
        self,
        metric: str,
        value: float,
        *,
        buckets: tuple[float, ...] = REQUEST_DURATION_BUCKETS,
        **labels: object,
    ) -> None:
        """Record one observation into a fixed-bound histogram series.

        ``buckets`` are ascending ``le`` upper bounds and are pinned to the first
        set seen for ``metric``; a later, differing set is rejected so the
        exposed histogram never re-buckets mid-process. Non-finite or negative
        observations are dropped rather than corrupting the sum. Label safety is
        the same as :meth:`increment`.
        """

        if not _METRIC_NAME_RE.fullmatch(metric):
            raise ValueError(f"Unsafe telemetry metric name: {metric!r}")
        numeric = float(value)
        if math.isnan(numeric) or math.isinf(numeric) or numeric < 0:
            return
        key = (metric, _label_key(labels))
        with self._lock:
            histogram = self._histograms.get(key)
            if histogram is None:
                pinned = self._histogram_bounds.get(metric)
                requested = tuple(float(bound) for bound in buckets)
                if pinned is None:
                    self._validate_bounds(requested)
                    pinned = requested
                    self._histogram_bounds[metric] = pinned
                elif requested != pinned:
                    raise ValueError(
                        f"Inconsistent histogram bounds for metric {metric!r}"
                    )
                histogram = _Histogram(pinned)
                self._histograms[key] = histogram
            histogram.observe(numeric)

    @staticmethod
    def _validate_bounds(bounds: tuple[float, ...]) -> None:
        if not bounds:
            raise ValueError("A histogram needs at least one bucket bound")
        if any(later <= earlier for earlier, later in zip(bounds, bounds[1:])):
            raise ValueError("Histogram bounds must be strictly ascending")

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

    def histogram_samples(
        self,
    ) -> list[
        tuple[
            str,
            tuple[tuple[str, str], ...],
            tuple[float, ...],
            list[int],
            float,
            int,
        ]
    ]:
        """Return ``(metric, labels, bounds, bucket_counts, sum, count)`` tuples.

        ``bucket_counts`` holds one non-cumulative count per finite bound plus a
        trailing +Inf overflow count; an exporter accumulates them into
        cumulative ``le`` buckets for the Prometheus exposition format.
        """

        with self._lock:
            return [
                (
                    metric,
                    labels,
                    histogram.bounds,
                    list(histogram.counts),
                    histogram.sum,
                    histogram.count,
                )
                for (metric, labels), histogram in self._histograms.items()
            ]

    def reset(self) -> None:
        """Clear all counters and histograms (used by tests)."""

        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._histogram_bounds.clear()

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

    def record_request_duration(
        self, seconds: float, status: OutcomeStatus
    ) -> None:
        """Record end-to-end request latency (seconds) bucketed by terminal status.

        Deliberately labelled by ``status`` only (never the failure ``code``) so
        the histogram's bucket count stays low-cardinality; the outcome counter
        carries the finer code breakdown.
        """

        self.observe(METRIC_REQUEST_DURATION, seconds, status=status)

    def record_auth_outcome(
        self,
        status: OutcomeStatus,
        failure_code: Optional[FailureCode] = None,
    ) -> None:
        """Record a terminal authentication attempt without user identifiers."""

        labels: dict[str, object] = {"status": status}
        if failure_code is not None:
            labels["code"] = failure_code
        self.increment(METRIC_AUTH_OUTCOME, **labels)

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

    def record_acl_reconciliation(
        self, action: str, state: str, count: int = 1
    ) -> None:
        """Record privacy-safe ACL audit/remediation counts (AUTH-10)."""

        if count > 0:
            self.increment(
                METRIC_ACL_RECONCILIATION,
                value=count,
                action=action,
                state=state,
            )

    def record_ingest_outcome(
        self, scope: str, status: IngestTerminalStatus | str
    ) -> None:
        """Record a file/run terminal state without file identifiers."""

        self.increment(METRIC_INGEST_OUTCOME, scope=scope, status=status)

    def record_ingest_review(self, reason: str) -> None:
        """Record why a file needs review (INGEST-08) without identifiers."""

        self.increment(METRIC_INGEST_REVIEW, reason=reason)

    def record_ingest_integrity(self, event: str, state: str) -> None:
        """Record registry, rollback, or reconciliation state transitions."""

        self.increment(METRIC_INGEST_INTEGRITY, event=event, state=state)

    def record_stale_writer_rejection(self, reason: str) -> None:
        """Record a token-guard rejection of a stale/invalid file transition.

        Emitted whenever the file registry rejects a terminal (or processing)
        transition because the processing/transition token is stale or the new
        status would overwrite a newer state (VECTOR-13). ``reason`` is a stable,
        low-cardinality label, never a file identifier or exception string.
        """

        self.increment(METRIC_INGEST_STALE_WRITER, reason=reason)


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
    "COMPONENT_CONVERSATION_MEMORY",
    "COMPONENT_INTENT_CLASSIFIER",
    "COMPONENT_OPENAI",
    "COMPONENT_OBSERVABILITY",
    "COMPONENT_PINECONE",
    "COMPONENT_RATE_LIMITER",
    "COMPONENT_REDIS",
    "COMPONENT_RESOURCE_LEASE",
    "COMPONENT_RETRIEVAL",
    "METRIC_ACL_METADATA_DROP",
    "METRIC_ACL_RECONCILIATION",
    "METRIC_FALLBACK_ACTIVATED",
    "METRIC_AUTH_OUTCOME",
    "METRIC_REQUEST_DURATION",
    "METRIC_REQUEST_OUTCOME",
    "METRIC_INGEST_OUTCOME",
    "REQUEST_DURATION_BUCKETS",
    "METRIC_INGEST_INTEGRITY",
    "METRIC_INGEST_STALE_WRITER",
    "METRIC_SERVICE_DEGRADED",
    "METRIC_SOURCE_OUTCOME",
    "Readiness",
    "ReadinessRegistry",
    "Telemetry",
    "get_readiness",
    "get_telemetry",
]
