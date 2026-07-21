#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Outcome-metric alert rules and in-process evaluation (Phase 5).

Phase 5 asks for "dashboards and alerts for the new outcome metrics". This
module holds a single declarative definition of the alert conditions so two
consumers stay in sync:

* :func:`render_prometheus_rules` emits a Prometheus alerting-rules YAML artifact
  (``ops/askalfred_alerts.yml``) for a real monitoring stack.
* :func:`evaluate_alerts` evaluates the same conditions in-process against the
  current :class:`~core.telemetry.Telemetry` / readiness state, so the operator
  diagnostics panel and tests can show which alerts are active without scraping.

The two representations are kept close on purpose: the PromQL is the source of
truth for a monitoring stack; the Python predicate is a best-effort, snapshot
approximation for the in-app operator view. Neither introduces high-cardinality
labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from core.outcomes import OutcomeStatus
from core.telemetry import (
    METRIC_ACL_METADATA_DROP,
    METRIC_INGEST_OUTCOME,
    METRIC_REQUEST_OUTCOME,
    METRIC_SERVICE_DEGRADED,
    Readiness,
    ReadinessRegistry,
    Telemetry,
    get_readiness,
    get_telemetry,
)

# Share of request outcomes that must be unavailable/failed before the elevated
# error-rate alert fires, and the minimum volume before the ratio is meaningful.
ERROR_RATE_THRESHOLD = 0.2
ERROR_RATE_MIN_VOLUME = 20

SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


def _sum_where(telemetry: Telemetry, metric: str, **required: str) -> int:
    """Sum counter samples of ``metric`` whose labels include ``required``.

    ``Telemetry.get`` matches an exact label set; alert conditions need to sum
    across the other labels (e.g. every failure ``code`` for a given
    ``status``), so this does a subset match over the raw samples.
    """

    total = 0
    wanted = {name: str(value) for name, value in required.items()}
    for sample_metric, labels, count in telemetry.samples():
        if sample_metric != metric:
            continue
        label_map = dict(labels)
        if all(label_map.get(name) == value for name, value in wanted.items()):
            total += count
    return total


def _request_total(telemetry: Telemetry) -> int:
    return _sum_where(telemetry, METRIC_REQUEST_OUTCOME)


@dataclass(frozen=True)
class AlertRule:
    """One alert condition in both PromQL and in-process forms."""

    name: str
    severity: str
    summary: str
    description: str
    promql: str
    for_duration: str
    predicate: Callable[[Telemetry, ReadinessRegistry], bool]


@dataclass(frozen=True)
class ActiveAlert:
    """An alert rule currently evaluating true in-process."""

    name: str
    severity: str
    summary: str


# ---------------------------------------------------------------------------
# In-process predicates
# ---------------------------------------------------------------------------


def _critical_inconsistent_requests(
    telemetry: Telemetry, _readiness: ReadinessRegistry
) -> bool:
    return (
        _sum_where(
            telemetry,
            METRIC_REQUEST_OUTCOME,
            status=OutcomeStatus.CRITICAL_INCONSISTENT.value,
        )
        > 0
    )


def _critical_inconsistent_ingest(
    telemetry: Telemetry, _readiness: ReadinessRegistry
) -> bool:
    return (
        _sum_where(
            telemetry,
            METRIC_INGEST_OUTCOME,
            status=OutcomeStatus.CRITICAL_INCONSISTENT.value,
        )
        > 0
    )


def _any_component_unavailable(
    _telemetry: Telemetry, readiness: ReadinessRegistry
) -> bool:
    return any(
        state.get("readiness") == Readiness.UNAVAILABLE.value
        for state in readiness.snapshot().values()
    )


def _any_component_degraded(
    _telemetry: Telemetry, readiness: ReadinessRegistry
) -> bool:
    return any(
        state.get("readiness") == Readiness.DEGRADED.value
        for state in readiness.snapshot().values()
    )


def _elevated_error_rate(
    telemetry: Telemetry, _readiness: ReadinessRegistry
) -> bool:
    total = _request_total(telemetry)
    if total < ERROR_RATE_MIN_VOLUME:
        return False
    errors = _sum_where(
        telemetry, METRIC_REQUEST_OUTCOME, status=OutcomeStatus.UNAVAILABLE.value
    ) + _sum_where(
        telemetry, METRIC_REQUEST_OUTCOME, status=OutcomeStatus.FAILED.value
    )
    return (errors / total) > ERROR_RATE_THRESHOLD


def _service_degraded(telemetry: Telemetry, _readiness: ReadinessRegistry) -> bool:
    return _sum_where(telemetry, METRIC_SERVICE_DEGRADED) > 0


def _acl_metadata_drop(telemetry: Telemetry, _readiness: ReadinessRegistry) -> bool:
    return _sum_where(telemetry, METRIC_ACL_METADATA_DROP) > 0


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------

_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        name="AskAlfredCriticalInconsistentRequest",
        severity=SEVERITY_CRITICAL,
        summary="A request ended in critical_inconsistent state",
        description=(
            "One or more user requests returned critical_inconsistent, meaning "
            "stored state may be inconsistent. Page an operator and reconcile."
        ),
        promql=(
            'sum(increase(askalfred_request_outcome_total'
            '{status="critical_inconsistent"}[10m])) > 0'
        ),
        for_duration="0m",
        predicate=_critical_inconsistent_requests,
    ),
    AlertRule(
        name="AskAlfredCriticalInconsistentIngest",
        severity=SEVERITY_CRITICAL,
        summary="An ingestion run ended in critical_inconsistent state",
        description=(
            "An ingestion file or run reached critical_inconsistent (e.g. an "
            "incomplete FRA rollback). Affected writes are blocked pending "
            "reconciliation."
        ),
        promql=(
            'sum(increase(askalfred_ingest_outcome_total'
            '{status="critical_inconsistent"}[30m])) > 0'
        ),
        for_duration="0m",
        predicate=_critical_inconsistent_ingest,
    ),
    AlertRule(
        name="AskAlfredComponentUnavailable",
        severity=SEVERITY_CRITICAL,
        summary="A required component is unavailable",
        description=(
            "A named component reported unavailable readiness. Retrieval, "
            "answer generation, or access control may be down."
        ),
        promql='max(askalfred_component_readiness{readiness="unavailable"}) > 0',
        for_duration="5m",
        predicate=_any_component_unavailable,
    ),
    AlertRule(
        name="AskAlfredComponentDegraded",
        severity=SEVERITY_WARNING,
        summary="A component is running degraded",
        description=(
            "A named component reported degraded readiness (reduced-capability "
            "fallback active). Results may be incomplete."
        ),
        promql='max(askalfred_component_readiness{readiness="degraded"}) > 0',
        for_duration="10m",
        predicate=_any_component_degraded,
    ),
    AlertRule(
        name="AskAlfredElevatedErrorRate",
        severity=SEVERITY_WARNING,
        summary="Elevated unavailable/failed request rate",
        description=(
            "The share of requests ending unavailable or failed exceeded "
            f"{int(ERROR_RATE_THRESHOLD * 100)}% of recent traffic."
        ),
        promql=(
            'sum(increase(askalfred_request_outcome_total'
            '{status=~"unavailable|failed"}[10m])) '
            "/ clamp_min(sum(increase(askalfred_request_outcome_total[10m])), 1) "
            f"> {ERROR_RATE_THRESHOLD}"
        ),
        for_duration="10m",
        predicate=_elevated_error_rate,
    ),
    AlertRule(
        name="AskAlfredServiceDegradedEvents",
        severity=SEVERITY_WARNING,
        summary="Degraded-service events observed",
        description=(
            "A backend (e.g. the rate-limit store) failed open or degraded. "
            "Safeguards may be running in a reduced mode."
        ),
        promql="sum(increase(askalfred_service_degraded_total[10m])) > 0",
        for_duration="5m",
        predicate=_service_degraded,
    ),
    AlertRule(
        name="AskAlfredAclMetadataDrops",
        severity=SEVERITY_WARNING,
        summary="Matches dropped for missing ACL metadata",
        description=(
            "Vectors were dropped during access filtering for missing/invalid "
            "ACL metadata. Identify and re-ingest/quarantine non-conformant "
            "vectors."
        ),
        promql="sum(increase(askalfred_acl_metadata_drop_total[30m])) > 0",
        for_duration="0m",
        predicate=_acl_metadata_drop,
    ),
)


def default_alert_rules() -> tuple[AlertRule, ...]:
    """Return the built-in alert rules."""

    return _RULES


def evaluate_alerts(
    telemetry: Optional[Telemetry] = None,
    readiness: Optional[ReadinessRegistry] = None,
    rules: Optional[tuple[AlertRule, ...]] = None,
) -> list[ActiveAlert]:
    """Return the alerts whose in-process predicate is currently true."""

    telemetry = telemetry or get_telemetry()
    readiness = readiness or get_readiness()
    active: list[ActiveAlert] = []
    for rule in rules or _RULES:
        try:
            fired = rule.predicate(telemetry, readiness)
        except Exception:  # pylint: disable=broad-except
            # An evaluation bug must not break the operator panel.
            fired = False
        if fired:
            active.append(
                ActiveAlert(
                    name=rule.name, severity=rule.severity, summary=rule.summary
                )
            )
    return active


def _yaml_quote(value: str) -> str:
    """Return a double-quoted YAML scalar."""

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_prometheus_rules(
    rules: Optional[tuple[AlertRule, ...]] = None,
    group_name: str = "askalfred_outcome_alerts",
) -> str:
    """Render the alert rules as a Prometheus alerting-rules YAML document."""

    rules = rules or _RULES
    lines = ["groups:", f"  - name: {group_name}", "    rules:"]
    for rule in rules:
        lines.append(f"      - alert: {rule.name}")
        lines.append(f"        expr: {rule.promql}")
        lines.append(f"        for: {rule.for_duration}")
        lines.append("        labels:")
        lines.append(f"          severity: {rule.severity}")
        lines.append("        annotations:")
        lines.append(f"          summary: {_yaml_quote(rule.summary)}")
        lines.append(f"          description: {_yaml_quote(rule.description)}")
    return "\n".join(lines) + "\n"


__all__ = [
    "ActiveAlert",
    "AlertRule",
    "ERROR_RATE_MIN_VOLUME",
    "ERROR_RATE_THRESHOLD",
    "SEVERITY_CRITICAL",
    "SEVERITY_WARNING",
    "default_alert_rules",
    "evaluate_alerts",
    "render_prometheus_rules",
]
