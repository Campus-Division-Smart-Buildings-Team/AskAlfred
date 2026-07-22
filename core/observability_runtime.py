#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Process-wide logging and service-metrics publishing for AskAlfred.

Streamlit executes the application script once per session rerun. Runtime
observability therefore lives in this imported module so file handlers and
background publishers are shared by every session in the Python process.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from collections.abc import Callable, Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from core.service_metrics import write_service_metrics

LOGGER = logging.getLogger(__name__)

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
DEFAULT_METRICS_INTERVAL_SECONDS = 15.0

_FILE_HANDLER_MARKER = "_askalfred_rotating_file_handler"
_RUNTIME_LOCK = threading.Lock()
_PUBLISHER: Optional["ServiceMetricsPublisher"] = None
_ATEXIT_REGISTERED = False


def _normalise_path(path: str | os.PathLike[str]) -> str:
    """Return a case-normalised absolute path without requiring it to exist."""

    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        LOGGER.warning("Ignoring invalid %s value; using %d", name, default)
        return default
    if parsed < 1:
        LOGGER.warning("Ignoring non-positive %s value; using %d", name, default)
        return default
    return parsed


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        LOGGER.warning("Ignoring invalid %s value; using %.1f", name, default)
        return default
    if parsed <= 0:
        LOGGER.warning("Ignoring non-positive %s value; using %.1f", name, default)
        return default
    return parsed


def _filter_key(log_filter: logging.Filter) -> str:
    """Return a stable key across Streamlit script-class redefinitions."""

    explicit = getattr(log_filter, "_askalfred_filter_key", None)
    if explicit:
        return str(explicit)
    cls = log_filter.__class__
    return f"{cls.__module__}.{cls.__name__}"


def _add_filters_once(
    handler: logging.Handler,
    filters: Iterable[logging.Filter],
) -> None:
    existing = {_filter_key(item) for item in handler.filters}
    for log_filter in filters:
        key = _filter_key(log_filter)
        if key not in existing:
            handler.addFilter(log_filter)
            existing.add(key)


def configure_rotating_file_logging(
    *,
    logger: Optional[logging.Logger] = None,
    output_path: Optional[str] = None,
    formatter: Optional[logging.Formatter] = None,
    filters: Iterable[logging.Filter] = (),
    max_bytes: Optional[int] = None,
    backup_count: Optional[int] = None,
) -> Optional[RotatingFileHandler]:
    """Attach exactly one bounded AskAlfred file handler to ``logger``.

    An empty ``ASKALFRED_LOG_FILE`` disables file logging. Repeated calls are
    idempotent, which is essential when Streamlit reruns ``main.py``.
    """

    target_logger = logger or logging.getLogger()
    configured_path = (
        output_path if output_path is not None else os.getenv("ASKALFRED_LOG_FILE", "")
    )
    configured_path = configured_path.strip()
    if not configured_path:
        return None

    normalised_target = _normalise_path(configured_path)
    previous_handlers: list[logging.Handler] = []
    for handler in list(target_logger.handlers):
        if not getattr(handler, _FILE_HANDLER_MARKER, False):
            continue
        if _normalise_path(getattr(handler, "baseFilename", "")) == normalised_target:
            if formatter is not None:
                handler.setFormatter(formatter)
            _add_filters_once(handler, filters)
            return handler if isinstance(handler, RotatingFileHandler) else None
        previous_handlers.append(handler)

    path = Path(configured_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=(
                max_bytes
                if max_bytes is not None
                else _positive_int_env("ASKALFRED_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES)
            ),
            backupCount=(
                backup_count
                if backup_count is not None
                else _positive_int_env(
                    "ASKALFRED_LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT
                )
            ),
            encoding="utf-8",
            delay=True,
        )
    except (OSError, ValueError) as error:
        LOGGER.error("Could not configure AskAlfred file logging: %s", error)
        return None

    setattr(file_handler, _FILE_HANDLER_MARKER, True)
    if formatter is not None:
        file_handler.setFormatter(formatter)
    _add_filters_once(file_handler, filters)
    target_logger.addHandler(file_handler)

    for old_handler in previous_handlers:
        target_logger.removeHandler(old_handler)
        try:
            old_handler.close()
        except OSError:
            pass

    return file_handler


class ServiceMetricsPublisher:
    """Periodically snapshot process telemetry to a Prometheus textfile."""

    def __init__(
        self,
        output_path: str,
        *,
        interval_seconds: float = DEFAULT_METRICS_INTERVAL_SECONDS,
        writer: Callable[[str], None] = write_service_metrics,
    ) -> None:
        if not output_path.strip():
            raise ValueError("output_path must not be empty")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.output_path = output_path
        self.interval_seconds = float(interval_seconds)
        self._writer = writer
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> None:
        """Write immediately and start the single daemon publishing thread."""

        with self._lifecycle_lock:
            if self.is_running:
                return
            self._stop_event.clear()
            self._publish_once()
            self._thread = threading.Thread(
                target=self._run,
                name="askalfred-service-metrics-publisher",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Request publisher shutdown and wait briefly for the daemon thread."""

        with self._lifecycle_lock:
            thread = self._thread
            self._stop_event.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self._publish_once()

    def _publish_once(self) -> None:
        try:
            self._writer(self.output_path)
        except (OSError, ValueError) as error:
            # Expected filesystem/configuration failures are retryable and must
            # not terminate the daemon thread. Programming defects remain loud.
            signature = f"{type(error).__name__}: {error}"
            if signature != self._last_error:
                LOGGER.error("Service metrics snapshot failed: %s", error)
            self._last_error = signature
            return

        if self._last_error is not None:
            LOGGER.info("Service metrics snapshot recovered")
        self._last_error = None


def start_service_metrics_publisher(
    *,
    output_path: Optional[str] = None,
    interval_seconds: Optional[float] = None,
) -> Optional[ServiceMetricsPublisher]:
    """Start or return the one publisher shared by all Streamlit sessions."""

    global _ATEXIT_REGISTERED, _PUBLISHER

    configured_path = (
        output_path
        if output_path is not None
        else os.getenv("SERVICE_METRICS_FILE", "")
    ).strip()
    if not configured_path:
        return None
    configured_interval = (
        float(interval_seconds)
        if interval_seconds is not None
        else _positive_float_env(
            "SERVICE_METRICS_INTERVAL_SECONDS", DEFAULT_METRICS_INTERVAL_SECONDS
        )
    )

    with _RUNTIME_LOCK:
        if (
            _PUBLISHER is not None
            and _normalise_path(_PUBLISHER.output_path)
            == _normalise_path(configured_path)
            and _PUBLISHER.interval_seconds == configured_interval
        ):
            _PUBLISHER.start()
            return _PUBLISHER

        if _PUBLISHER is not None:
            _PUBLISHER.stop()

        _PUBLISHER = ServiceMetricsPublisher(
            configured_path,
            interval_seconds=configured_interval,
        )
        _PUBLISHER.start()
        if not _ATEXIT_REGISTERED:
            atexit.register(stop_service_metrics_publisher)
            _ATEXIT_REGISTERED = True
        return _PUBLISHER


def stop_service_metrics_publisher() -> None:
    """Stop the process-wide publisher, if configured."""

    global _PUBLISHER
    with _RUNTIME_LOCK:
        publisher = _PUBLISHER
        _PUBLISHER = None
    if publisher is not None:
        publisher.stop()


__all__ = [
    "ServiceMetricsPublisher",
    "configure_rotating_file_logging",
    "start_service_metrics_publisher",
    "stop_service_metrics_publisher",
]
