"""Explicit degradation handling for ingestion observability boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from core.failure_codes import FailureCode
from core.ingest_outcomes import IngestTerminalStatus
from core.telemetry import (
    COMPONENT_OBSERVABILITY,
    get_readiness,
    get_telemetry,
)

if TYPE_CHECKING:
    from ingest.batch_ingest import IngestReport


ObservabilityChannel = Literal["event_export", "metrics_export"]

_FAILURE_STATE: dict[ObservabilityChannel, str] = {
    "event_export": "event_export_failed",
    "metrics_export": "metrics_export_failed",
}
_HEALTHY_RUN_STATUSES = frozenset(
    {
        IngestTerminalStatus.SUCCESS,
        IngestTerminalStatus.SUCCESS_WITH_SKIPS,
        IngestTerminalStatus.DRY_RUN,
    }
)


def mark_observability_degraded(
    ctx: Any,
    channel: ObservabilityChannel,
    *,
    report: IngestReport | None = None,
) -> None:
    """Publish one stable observability failure and annotate its run outcome."""

    state = _FAILURE_STATE[channel]
    ctx.stats.increment(f"observability_{channel}_failures_total")
    get_telemetry().record_ingest_integrity("observability", state)
    get_telemetry().record_service_degraded(
        COMPONENT_OBSERVABILITY,
        FailureCode.OBSERVABILITY_EXPORT_FAILED,
    )
    get_readiness().mark_degraded(
        COMPONENT_OBSERVABILITY,
        FailureCode.OBSERVABILITY_EXPORT_FAILED,
    )

    if report is None:
        return
    report.observability_degraded = True
    if channel not in report.observability_failures:
        report.observability_failures.append(channel)
    if report.status in _HEALTHY_RUN_STATUSES:
        report.status = IngestTerminalStatus.DEGRADED


def emit_event_safely(
    ctx: Any,
    event: dict[str, Any],
    *,
    description: str,
    report: IngestReport | None = None,
) -> bool:
    """Emit an event without coupling observability to the ingestion data path."""

    try:
        ctx.event_sink.emit_event(event)
    except Exception as error:  # pylint: disable=broad-except
        mark_observability_degraded(ctx, "event_export", report=report)
        retained = bool(getattr(error, "retained", False))
        retention = " (retained for replay)" if retained else ""
        ctx.logger.warning("%s failed%s: %s", description, retention, error)
        return False
    return True


__all__ = [
    "ObservabilityChannel",
    "emit_event_safely",
    "mark_observability_degraded",
]
