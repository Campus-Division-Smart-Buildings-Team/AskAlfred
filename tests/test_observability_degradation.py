"""Behavioral coverage for VECTOR-15 observability degradation."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.alfred_exceptions import ObservabilityError
from core.failure_codes import FailureCode
from core.ingest_outcomes import IngestTerminalStatus
from core.telemetry import (
    COMPONENT_OBSERVABILITY,
    METRIC_INGEST_INTEGRITY,
    METRIC_SERVICE_DEGRADED,
    Readiness,
    get_readiness,
    get_telemetry,
)
from ingest.batch_ingest import IngestReport, _emit_ingest_metrics
from ingest.transaction import ThreadSafeStats
from interfaces.event_sink import JsonlPrometheusEventSink


@pytest.fixture(autouse=True)
def _reset_observability_state():
    get_telemetry().reset()
    get_readiness().reset()
    yield
    get_telemetry().reset()
    get_readiness().reset()


def _report() -> IngestReport:
    return IngestReport(
        files_found=1,
        files_processed=1,
        files_skipped=0,
        files_failed=0,
        total_vectors=2,
        duration_seconds=1.0,
    )


def test_failed_event_is_durably_spooled_and_replayed_before_next_event(tmp_path):
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("occupied", encoding="utf-8")
    spool = tmp_path / "durable" / "events.jsonl"
    failed_destination = blocked_parent / "events.jsonl"

    failing_sink = JsonlPrometheusEventSink(
        events_path=str(failed_destination),
        spool_path=str(spool),
    )
    with pytest.raises(ObservabilityError) as caught:
        failing_sink.emit_event({"event_type": "retained", "sequence": 1})

    assert caught.value.retained is True
    assert json.loads(spool.read_text(encoding="utf-8"))["sequence"] == 1

    destination = tmp_path / "events" / "events.jsonl"
    recovered_sink = JsonlPrometheusEventSink(
        events_path=str(destination),
        spool_path=str(spool),
    )
    recovered_sink.emit_event({"event_type": "live", "sequence": 2})

    delivered = [
        json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["sequence"] for event in delivered] == [1, 2]
    assert spool.read_text(encoding="utf-8") == ""


def test_metrics_export_failure_marks_run_and_component_degraded(tmp_path):
    sink = Mock()
    sink.export_metrics.side_effect = OSError("collector unavailable")
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            prometheus_metrics_file=str(tmp_path / "metrics.prom"),
            export_events=False,
            dry_run=False,
            upsert_workers=1,
        ),
        event_sink=sink,
        logger=logging.getLogger("vector-15-metrics"),
        stats=ThreadSafeStats(),
    )
    report = _report()

    _emit_ingest_metrics(ctx, report, str(tmp_path))

    assert report.status is IngestTerminalStatus.DEGRADED
    assert report.observability_degraded is True
    assert report.observability_failures == ["metrics_export"]
    assert ctx.stats.get_stats()["observability_metrics_export_failures_total"] == 1
    assert (
        get_telemetry().get(
            METRIC_INGEST_INTEGRITY,
            event="observability",
            state="metrics_export_failed",
        )
        == 1
    )
    assert (
        get_telemetry().get(
            METRIC_SERVICE_DEGRADED,
            component=COMPONENT_OBSERVABILITY,
            code=FailureCode.OBSERVABILITY_EXPORT_FAILED,
        )
        == 1
    )
    assert get_readiness().get(COMPONENT_OBSERVABILITY) is Readiness.DEGRADED


def test_event_export_failure_is_explicit_and_keeps_data_path_independent(tmp_path):
    sink = Mock()
    sink.emit_event.side_effect = ObservabilityError(
        "destination unavailable", retained=True
    )
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            prometheus_metrics_file="",
            export_events=True,
            dry_run=False,
            upsert_workers=1,
        ),
        event_sink=sink,
        logger=logging.getLogger("vector-15-events"),
        stats=ThreadSafeStats(),
    )
    report = _report()

    _emit_ingest_metrics(ctx, report, str(tmp_path))

    assert report.status is IngestTerminalStatus.DEGRADED
    assert report.observability_failures == ["event_export"]
    assert ctx.stats.get_stats()["observability_event_export_failures_total"] == 1
    assert (
        get_telemetry().get(
            METRIC_INGEST_INTEGRITY,
            event="observability",
            state="event_export_failed",
        )
        == 1
    )
