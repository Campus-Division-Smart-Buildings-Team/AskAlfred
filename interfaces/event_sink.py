# EventSink port
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from core.alfred_exceptions import ObservabilityError
from ingest.utils import MetricsExporter


@runtime_checkable
class MetricsReader(Protocol):
    def get_stats(self) -> dict[str, Any]: ...


class EventSink(Protocol):
    def emit_event(self, event: dict[str, Any]) -> None: ...
    def export_metrics(
        self,
        *,
        stats: MetricsReader | Mapping[str, Any],
        output_path: str,
        duration_seconds: float,
        vectors_per_second: float,
        source_path: str,
        dry_run: bool,
        upsert_workers: int | None = None,
    ) -> None: ...


class JsonlPrometheusEventSink:
    """Writes JSONL events and Prometheus metrics.

    Event delivery is at-least-once.  A failed event append is durably written
    to a separate local spool, and the spool is replayed before the next live
    event.  A replay interrupted after delivery but before spool truncation can
    produce a duplicate, so consumers should treat events as idempotent.
    """

    def __init__(
        self,
        *,
        events_path: Optional[str] = None,
        spool_path: Optional[str] = None,
        lock: Optional[Lock] = None,
    ):
        self._events_path = (events_path or "").strip() or None
        configured_spool = (spool_path or "").strip() or None
        if configured_spool is None and self._events_path:
            configured_spool = self._events_path + ".spool"
        self._spool_path = configured_spool
        if self._events_path and self._spool_path:
            if Path(self._events_path).resolve() == Path(self._spool_path).resolve():
                raise ValueError("Event destination and spool must be different files")
        self._lock = lock or Lock()
        self._metrics = MetricsExporter()

    def emit_event(self, event: dict[str, Any]) -> None:
        if not self._events_path:
            return
        try:
            line = json.dumps(event, ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as error:
            raise ObservabilityError(
                "Event payload could not be serialised", retained=False
            ) from error

        with self._lock:
            try:
                self._replay_spooled_events_locked()
                self._durable_append(Path(self._events_path), line)
            except (OSError, ValueError) as export_error:
                retained = self._spool_line_locked(line)
                raise ObservabilityError(
                    "Event export failed"
                    + ("; retained for replay" if retained else "; retention failed"),
                    retained=retained,
                ) from export_error

    def replay_spooled_events(self) -> int:
        """Replay retained events and return the number delivered.

        The spool is retained unchanged when delivery fails, allowing a later
        process/run to retry it.
        """

        if not self._events_path or not self._spool_path:
            return 0
        with self._lock:
            try:
                return self._replay_spooled_events_locked()
            except (OSError, ValueError) as error:
                raise ObservabilityError(
                    "Event spool replay failed", retained=True
                ) from error

    @staticmethod
    def _durable_append(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    def _spool_line_locked(self, line: str) -> bool:
        if not self._spool_path:
            return False
        try:
            self._durable_append(Path(self._spool_path), line)
        except OSError:
            return False
        return True

    def _replay_spooled_events_locked(self) -> int:
        if not self._spool_path:
            return 0
        spool = Path(self._spool_path)
        try:
            payload = spool.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        if not payload:
            return 0

        # The spool contains one complete JSON object per durable append.
        count = sum(bool(line.strip()) for line in payload.splitlines())
        self._durable_append(Path(self._events_path), payload)
        with spool.open("w", encoding="utf-8") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        return count

    def export_metrics(
        self,
        *,
        stats: MetricsReader | Mapping[str, Any],
        output_path: str,
        duration_seconds: float,
        vectors_per_second: float,
        source_path: str,
        dry_run: bool,
        upsert_workers: int | None = None,
    ) -> None:
        if isinstance(stats, MetricsReader):
            stats_payload = stats.get_stats()
        else:
            stats_payload = dict(stats)
        self._metrics.export_prometheus(
            stats=stats_payload,
            output_path=output_path,
            duration_seconds=duration_seconds,
            vectors_per_second=vectors_per_second,
            source_path=source_path,
            dry_run=dry_run,
            upsert_workers=upsert_workers,
        )


__all__ = [
    "EventSink",
    "JsonlPrometheusEventSink",
]
