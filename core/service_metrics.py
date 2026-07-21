#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Query/service-side metrics exposition for dashboards and alerts (Phase 5).

Phase 4 already exports *ingestion* metrics to a Prometheus textfile. This module
is the query/service-side counterpart: it renders the process-wide
:class:`~core.telemetry.Telemetry` counters and the
:class:`~core.telemetry.ReadinessRegistry` states into Prometheus text-exposition
format so an operator dashboard can chart request outcomes, source outcomes,
fallbacks, degraded-service events, and ACL drops, and so alert rules
(:mod:`core.alerts`) can fire on them.

Only the low-cardinality, already-validated telemetry labels are exported; this
module never introduces exception text, IDs, queries, or paths as labels. The
readiness of each named component is exported as a gauge so a health probe can
read component state without inspecting logs (plan section H, Phase 3 → Phase 5
hand-off).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.telemetry import (
    METRIC_ACL_METADATA_DROP,
    METRIC_FALLBACK_ACTIVATED,
    METRIC_INGEST_INTEGRITY,
    METRIC_INGEST_OUTCOME,
    METRIC_REQUEST_OUTCOME,
    METRIC_SERVICE_DEGRADED,
    METRIC_SOURCE_OUTCOME,
    ReadinessRegistry,
    Telemetry,
    get_readiness,
    get_telemetry,
)

_PREFIX = "askalfred"
_READINESS_METRIC = f"{_PREFIX}_component_readiness"

# Human-readable help text keyed by the raw telemetry metric name.
_METRIC_HELP: dict[str, str] = {
    METRIC_REQUEST_OUTCOME: "User-facing request outcomes by terminal status and failure code.",
    METRIC_SOURCE_OUTCOME: "Per-source retrieval outcomes by component and status.",
    METRIC_FALLBACK_ACTIVATED: "Reduced-capability fallback activations by component.",
    METRIC_SERVICE_DEGRADED: "Degraded-service (fail-open/backend outage) events by component and code.",
    METRIC_ACL_METADATA_DROP: "Matches dropped for missing/invalid ACL metadata under an active filter.",
    METRIC_INGEST_OUTCOME: "Ingestion file/run terminal states by scope and status.",
    METRIC_INGEST_INTEGRITY: "Registry/rollback/reconciliation state transitions.",
}


def _escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus text exposition format."""

    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(
        f'{name}="{_escape_label_value(value)}"' for name, value in labels
    )
    return "{" + inner + "}"


def render_service_metrics(
    telemetry: Optional[Telemetry] = None,
    readiness: Optional[ReadinessRegistry] = None,
) -> str:
    """Render current service telemetry and readiness as Prometheus text.

    The output is safe to serve at ``/metrics`` or write to a node_exporter
    textfile. Metric names are prefixed with ``askalfred_`` and each counter
    family carries ``# HELP``/``# TYPE`` lines.
    """

    telemetry = telemetry or get_telemetry()
    readiness = readiness or get_readiness()

    # Group counter samples by their (prefixed) metric family name.
    families: dict[str, list[tuple[tuple[tuple[str, str], ...], int]]] = {}
    help_for: dict[str, str] = {}
    for metric, labels, count in telemetry.samples():
        family = f"{_PREFIX}_{metric}"
        families.setdefault(family, []).append((labels, count))
        help_for.setdefault(
            family, _METRIC_HELP.get(metric, f"{metric} counter.")
        )

    lines: list[str] = []
    for family in sorted(families):
        lines.append(f"# HELP {family} {help_for[family]}")
        lines.append(f"# TYPE {family} counter")
        for labels, count in sorted(families[family], key=lambda item: item[0]):
            lines.append(f"{family}{_render_labels(labels)} {count}")

    # Component readiness as a gauge: one line per component with its current
    # readiness carried as a label so a single series tracks health over time.
    readiness_snapshot = readiness.snapshot()
    if readiness_snapshot:
        lines.append(
            f"# HELP {_READINESS_METRIC} Component readiness "
            "(ready/degraded/unavailable) as a 1-valued gauge."
        )
        lines.append(f"# TYPE {_READINESS_METRIC} gauge")
        for component in sorted(readiness_snapshot):
            state = readiness_snapshot[component]
            label_pairs: list[tuple[str, str]] = [
                ("component", component),
                ("readiness", str(state.get("readiness", "ready"))),
            ]
            code = state.get("code")
            if code:
                label_pairs.append(("code", str(code)))
            lines.append(
                f"{_READINESS_METRIC}{_render_labels(tuple(label_pairs))} 1"
            )

    return "\n".join(lines) + "\n" if lines else ""


def write_service_metrics(
    output_path: str,
    telemetry: Optional[Telemetry] = None,
    readiness: Optional[ReadinessRegistry] = None,
) -> None:
    """Atomically write the rendered metrics to ``output_path``.

    The atomic temp-then-replace write means a Prometheus textfile collector
    never reads a half-written file.
    """

    text = render_service_metrics(telemetry, readiness)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out.with_suffix(out.suffix + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(out)


__all__ = [
    "render_service_metrics",
    "write_service_metrics",
]
