from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from core.observability_runtime import (
    ServiceMetricsPublisher,
    configure_rotating_file_logging,
)
from core.outcomes import OutcomeStatus
from core.service_metrics import write_service_metrics
from core.telemetry import ReadinessRegistry, Telemetry
from security.log_sanitiser import SanitisedFormatter


def _close_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_rotating_file_logging_is_idempotent_and_sanitised(tmp_path):
    logger = logging.Logger("askalfred-test-file-logging", level=logging.INFO)
    destination = tmp_path / "logs" / "askalfred.log"
    formatter = SanitisedFormatter("%(levelname)s %(message)s")

    try:
        first = configure_rotating_file_logging(
            logger=logger,
            output_path=str(destination),
            formatter=formatter,
            max_bytes=1024,
            backup_count=2,
        )
        second = configure_rotating_file_logging(
            logger=logger,
            output_path=str(destination),
            formatter=formatter,
            max_bytes=1024,
            backup_count=2,
        )

        assert first is not None
        assert second is first
        assert len(logger.handlers) == 1

        logger.info("openai_key=sk-1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh")
        first.flush()
        persisted = destination.read_text(encoding="utf-8")
        assert "sk-1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh" not in persisted
        assert "REDACTED" in persisted
    finally:
        _close_handlers(logger)


def test_rotating_file_logging_bounds_file_size(tmp_path):
    logger = logging.Logger("askalfred-test-log-rotation", level=logging.INFO)
    destination = tmp_path / "askalfred.log"

    try:
        handler = configure_rotating_file_logging(
            logger=logger,
            output_path=str(destination),
            formatter=logging.Formatter("%(message)s"),
            max_bytes=80,
            backup_count=2,
        )
        assert handler is not None

        for index in range(20):
            logger.info("message-%02d-xxxxxxxxxxxxxxxx", index)
        handler.flush()

        assert destination.exists()
        assert (tmp_path / "askalfred.log.1").exists()
        assert len(list(tmp_path.glob("askalfred.log*"))) <= 3
    finally:
        _close_handlers(logger)


def test_service_metrics_writes_are_safe_under_concurrency(tmp_path):
    telemetry = Telemetry()
    telemetry.record_request_outcome(OutcomeStatus.SUCCESS)
    destination = tmp_path / "service_metrics.prom"

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                write_service_metrics,
                str(destination),
                telemetry,
                ReadinessRegistry(),
            )
            for _ in range(40)
        ]
        for future in futures:
            future.result()

    persisted = destination.read_text(encoding="utf-8")
    assert 'askalfred_request_outcome_total{status="success"} 1' in persisted
    assert "askalfred_metrics_export_timestamp_seconds" in persisted
    assert persisted.endswith("\n")
    assert not list(tmp_path.glob(".service_metrics.prom.*.tmp"))


def test_metrics_publisher_is_single_start_and_stops_cleanly():
    calls: list[str] = []
    calls_lock = threading.Lock()
    published_twice = threading.Event()

    def writer(path: str) -> None:
        with calls_lock:
            calls.append(path)
            if len(calls) >= 2:
                published_twice.set()

    publisher = ServiceMetricsPublisher(
        "service_metrics.prom",
        interval_seconds=0.01,
        writer=writer,
    )
    try:
        publisher.start()
        first_thread = publisher._thread  # pylint: disable=protected-access
        publisher.start()
        assert publisher._thread is first_thread  # pylint: disable=protected-access
        assert published_twice.wait(timeout=1.0)
        assert publisher.is_running
    finally:
        publisher.stop()

    assert not publisher.is_running
    assert calls[0] == "service_metrics.prom"
