#!/usr/bin/env python3
"""
Batch Ingest for AskAlfred.
Core logic for batch ingestion, focused on local directory ingestion with progress tracking and security.
This module contains two main layers:
1. run_ingest() - Core ingest loop, independent of UI/progress concerns. Processes files, handles worker coordination, and returns an IngestReport.
2. ingest_local_directory_with_progress() - Thin wrapper around run_ingest() that handles local file discovery, building resolution, and progress tracking.
Also emits metrics and logs summary after completion.
"""

import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from building.filename_building_parser import (
    FilenameBuildingResolver,
    load_manual_building_overrides,
)
from config import (
    INGEST_UPSERT_JOIN_POLL_SECONDS,
    INGEST_UPSERT_JOIN_TIMEOUT_SECONDS,
)
from core.alfred_exceptions import (
    CriticalInconsistentError,
    ExternalServiceError,
    IngestError,
)
from core.fault_injection import FaultPoint, maybe_fail
from core.ingest_outcomes import IngestTerminalStatus, exit_code_for_status
from core.telemetry import get_telemetry
from security.file_operations_validator import (
    FileOperationSecurityError,
    validate_directory_safety,
)

from .context import IngestContext
from .document_content import (
    load_building_names_with_aliases,
)
from .document_processor import DocumentProcessor, FileIngestOrchestrator, Writer
from .helpers import UpsertQueueItem
from .transaction import (
    _mark_batch_failed,
    extract_fra_risk_items_integration,
    upsert_vectors_atomic,
)
from .upsert_handler import (
    VectorWriteCoordinator,
    _upsert_worker,
)
from .utils import (
    IngestionProgressTracker,
    list_local_files_secure,
)

# ---------------------------------------------------------------------------
# 7. IngestReport  +  run_ingest()  (core logic, no UI)
# ---------------------------------------------------------------------------


@dataclass
class IngestReport:
    """Value object returned by run_ingest()."""

    files_found: int
    files_processed: int
    files_skipped: int
    files_failed: int
    total_vectors: int
    duration_seconds: float
    failed_files: list[str] = field(default_factory=list)
    status: IngestTerminalStatus = IngestTerminalStatus.SUCCESS
    files_partial: int = 0
    files_unavailable: int = 0
    files_degraded: int = 0
    files_needs_review: int = 0
    review_reasons: dict[str, int] = field(default_factory=dict)
    file_outcomes: dict[str, str] = field(default_factory=dict)
    worker_queue_timed_out: bool = False
    lingering_workers: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return int(exit_code_for_status(self.status))

    @property
    def vectors_per_second(self) -> float:
        return (
            self.total_vectors / self.duration_seconds
            if self.duration_seconds > 0
            else 0.0
        )


@dataclass(frozen=True)
class WorkerTeardownReport:
    """Explicit result of draining and stopping background writers."""

    queue_timed_out: bool = False
    cancelled: bool = False
    aborted: bool = False
    lingering_workers: tuple[str, ...] = ()


def _status_for_exception(error: BaseException) -> IngestTerminalStatus:
    if isinstance(error, CriticalInconsistentError):
        return IngestTerminalStatus.CRITICAL_INCONSISTENT
    if isinstance(error, ExternalServiceError):
        return IngestTerminalStatus.UNAVAILABLE
    return IngestTerminalStatus.FAILED


def _derive_run_status(
    *,
    ctx: IngestContext,
    files_found: int,
    file_outcomes: dict[str, str],
    upsert_errors: list[Exception],
    teardown: WorkerTeardownReport,
) -> IngestTerminalStatus:
    if getattr(ctx.config, "dry_run", False):
        return IngestTerminalStatus.DRY_RUN
    if files_found == 0:
        return IngestTerminalStatus.EMPTY_INPUT
    states = set(file_outcomes.values())
    if "critical_inconsistent" in states or ctx.stats.get_stats().get(
        "critical_inconsistent_total", 0
    ):
        return IngestTerminalStatus.CRITICAL_INCONSISTENT
    if teardown.cancelled:
        return IngestTerminalStatus.CANCELLED
    successful = bool(
        states & {"success", "success_with_skips", "skipped", "degraded"}
    )
    incomplete = bool(states & {"partial", "unavailable", "failed", "cancelled"})
    stats = ctx.stats.get_stats()
    if stats.get("registry_divergence_total", 0):
        return IngestTerminalStatus.PARTIAL
    if upsert_errors or teardown.queue_timed_out or teardown.lingering_workers:
        return IngestTerminalStatus.PARTIAL if successful else IngestTerminalStatus.FAILED
    if "cancelled" in states:
        return IngestTerminalStatus.CANCELLED
    if "partial" in states or (successful and incomplete):
        return IngestTerminalStatus.PARTIAL
    if "unavailable" in states:
        return IngestTerminalStatus.PARTIAL if successful else IngestTerminalStatus.UNAVAILABLE
    if "failed" in states:
        return IngestTerminalStatus.PARTIAL if successful else IngestTerminalStatus.FAILED
    if states == {"needs_review"}:
        return IngestTerminalStatus.NEEDS_REVIEW
    if "needs_review" in states and successful:
        return IngestTerminalStatus.PARTIAL
    # Every file completed, but at least one only through a lossy encoding
    # fallback: the run committed all its vectors yet cannot claim full
    # fidelity (INGEST-06).
    if "degraded" in states:
        return IngestTerminalStatus.DEGRADED
    if "skipped" in states or int(stats.get("files_skipped", 0)):
        return IngestTerminalStatus.SUCCESS_WITH_SKIPS
    return IngestTerminalStatus.SUCCESS


def _run_ingest_sequential(
    ctx: IngestContext,
    objs: list[dict[str, Any]],
    orchestrator: FileIngestOrchestrator,
    coordinator: VectorWriteCoordinator,
    upsert_stop_event: threading.Event,
    progress: IngestionProgressTracker | None = None,
) -> None:
    """Process files sequentially."""
    for index, obj in enumerate(objs):
        if upsert_stop_event.is_set():
            for pending_obj in objs[index:]:
                pending_key = str(
                    pending_obj.get("Key") or pending_obj.get("key") or ""
                )
                if pending_key:
                    ctx.stats.record_file_terminal(pending_key, "cancelled")
            break
        filename = obj.get("Key", "")
        try:
            result = orchestrator.process(obj)
            if result.vectors:
                coordinator.add_vectors(result.vectors, progress=progress)
            if progress:
                progress.update(
                    filename,
                    vectors=result.vector_count,
                    status=result.status,
                )
            ctx.stats.increment("files_processed")
            if result.status == "skipped":
                ctx.stats.increment("files_skipped")
                ctx.stats.record_file_terminal(filename, "skipped")
            elif getattr(ctx.config, "dry_run", False):
                ctx.stats.record_file_terminal(filename, "dry_run")
            elif result.status in {"needs_review", "partial", "cancelled"}:
                ctx.stats.record_file_terminal(
                    filename, result.status, result.review_reason
                )
        except Exception as error:  # pylint: disable=broad-except
            ctx.logger.warning("Failed to ingest file %s: %s", obj.get("Key"), error)
            ctx.stats.increment("files_failed")
            failed_key = obj.get("Key") or obj.get("key") or ""
            if failed_key:
                ctx.stats.append_failed(failed_key)
                terminal = _status_for_exception(error)
                ctx.stats.record_file_terminal(failed_key, terminal.value)
                try:
                    orchestrator.mark_failed(
                        obj,
                        terminal.value,
                        status=terminal.value,
                    )
                except Exception as registry_error:  # pylint: disable=broad-except
                    ctx.logger.error(
                        "Could not finish failed file state: %s", registry_error
                    )
                    ctx.stats.increment("registry_divergence_total")
            ctx.stats.increment("files_processed")
            if progress:
                progress.update(filename, status="failed")


def _run_ingest_parallel(
    ctx: IngestContext,
    objs: list[dict[str, Any]],
    orchestrator: FileIngestOrchestrator,
    coordinator: VectorWriteCoordinator,
    upsert_stop_event: threading.Event,
    progress: IngestionProgressTracker | None = None,
) -> None:
    """
    Process files in parallel using thread pool.

    Rate limiting is enforced through:
    - Limited max_io_workers (default 12, max 32) to prevent DOS
    - Limited max_parse_workers (default 6, max 16) to prevent DOS
    - ThreadPoolExecutor bounds workers to prevent resource exhaustion
    """
    # The thread pool sizes *I/O* concurrency: each worker blocks on Redis,
    # the OpenAI embeddings call and the upsert queue. CPU-bound
    # extract/chunk/FRA parsing is offloaded to the separate max_parse_workers
    # process pool, so it must not widen this pool — doing so would silently
    # raise the number of concurrent embedding calls above the configured I/O
    # limit (the knob operators tune against API rate limits).
    worker_count = ctx.config.max_io_workers
    # DOS protection: Cap worker count to prevent resource exhaustion
    worker_count = min(worker_count, 32)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(orchestrator.process, obj): obj for obj in objs}
        aborting = False
        for future in as_completed(futures):
            if upsert_stop_event.is_set():
                aborting = True
                for pending_future in futures:
                    if not pending_future.done():
                        pending_future.cancel()
            obj = futures[future]
            filename = obj.get("Key", "")
            if aborting:
                try:
                    future.result()
                except Exception:  # pylint: disable=broad-except
                    pass
                if filename:
                    ctx.stats.record_file_terminal(filename, "cancelled")
                try:
                    orchestrator.mark_failed(
                        obj, "worker_aborted", status="cancelled"
                    )
                except Exception as registry_error:  # pylint: disable=broad-except
                    ctx.logger.error(
                        "Could not finish cancelled file state: %s",
                        registry_error,
                    )
                    ctx.stats.increment("registry_divergence_total")
                ctx.stats.increment("files_processed")
                continue
            try:
                result = future.result()
                if result.vectors:
                    coordinator.add_vectors(result.vectors, progress=progress)
                if progress:
                    progress.update(
                        filename,
                        vectors=result.vector_count,
                        status=result.status,
                    )
                ctx.stats.increment("files_processed")
                if result.status == "skipped":
                    ctx.stats.increment("files_skipped")
                    ctx.stats.record_file_terminal(filename, "skipped")
                elif getattr(ctx.config, "dry_run", False):
                    ctx.stats.record_file_terminal(filename, "dry_run")
                elif result.status in {"needs_review", "partial", "cancelled"}:
                    ctx.stats.record_file_terminal(
                        filename, result.status, result.review_reason
                    )
            except Exception as error:  # pylint: disable=broad-except
                ctx.logger.warning(
                    "Failed to ingest file %s: %s", obj.get("Key"), error
                )
                ctx.stats.increment("files_failed")
                failed_key = obj.get("Key") or obj.get("key") or ""
                if failed_key:
                    ctx.stats.append_failed(failed_key)
                    terminal = _status_for_exception(error)
                    ctx.stats.record_file_terminal(failed_key, terminal.value)
                    try:
                        orchestrator.mark_failed(
                            obj,
                            terminal.value,
                            status=terminal.value,
                        )
                    except Exception as registry_error:  # pylint: disable=broad-except
                        ctx.logger.error(
                            "Could not finish failed file state: %s", registry_error
                        )
                        ctx.stats.increment("registry_divergence_total")
                ctx.stats.increment("files_processed")
                if progress:
                    progress.update(filename, status="failed")


def run_ingest(
    ctx: IngestContext,
    objs: list[dict[str, Any]],
    *,
    orchestrator: FileIngestOrchestrator,
    coordinator: VectorWriteCoordinator,
    upsert_stop_event: threading.Event,
    upsert_queue: Queue[UpsertQueueItem | None] | None,
    upsert_threads: list[threading.Thread] | None,
    upsert_errors: list[Exception],
    use_worker: bool,
    base_path: str,
    progress: IngestionProgressTracker | None = None,
) -> IngestReport:
    """
    Core ingest loop — no progress bars, no UI concerns.

    Iterates *objs*, feeds vectors into *coordinator*, and handles
    worker teardown.  Returns an IngestReport.

    Progress updates are delegated to *progress* if provided.
    """
    t_start = time.time()

    if ctx.config.max_io_workers == 1 and ctx.config.max_parse_workers == 1:
        _run_ingest_sequential(
            ctx, objs, orchestrator, coordinator, upsert_stop_event, progress
        )
    else:
        _run_ingest_parallel(
            ctx, objs, orchestrator, coordinator, upsert_stop_event, progress
        )

    # Close coordinator and handle abort/normal paths
    if upsert_stop_event.is_set():
        # Abort path: drain pending work and mark failed
        pending = coordinator.drain_pending_vectors()
        _mark_batch_failed(ctx, pending, reason="upsert_worker_failed")
    else:
        # Normal path: flush all batches to queue/inline
        coordinator.close(progress=progress)

    # Tear down the worker thread (coordinator already closed)
    teardown = WorkerTeardownReport()
    if use_worker and upsert_queue is not None and upsert_threads:
        teardown = _teardown_worker(
            ctx,
            upsert_stop_event=upsert_stop_event,
            upsert_queue=upsert_queue,
            upsert_threads=upsert_threads,
        )

    # Check for worker errors
    if upsert_errors:
        ctx.logger.error("Upsert worker reported %d error(s)", len(upsert_errors))
        for error in upsert_errors:
            ctx.logger.error("  - %s", error)

    stats = ctx.stats.get_stats()
    duration = time.time() - t_start
    file_outcomes = dict(stats.get("file_terminal_states", {}))
    run_status = _derive_run_status(
        ctx=ctx,
        files_found=len(objs),
        file_outcomes=file_outcomes,
        upsert_errors=upsert_errors,
        teardown=teardown,
    )
    get_telemetry().record_ingest_outcome("run", run_status)
    files_failed = sum(
        status in {"failed", "cancelled", "critical_inconsistent"}
        for status in file_outcomes.values()
    )
    return IngestReport(
        files_found=len(objs),
        files_processed=stats["files_processed"],
        files_skipped=stats["files_skipped"],
        files_failed=max(int(stats["files_failed"]), files_failed),
        total_vectors=stats["total_vectors"],
        duration_seconds=duration,
        failed_files=list(stats.get("failed_files", [])),
        status=run_status,
        files_partial=sum(status == "partial" for status in file_outcomes.values()),
        files_unavailable=sum(
            status == "unavailable" for status in file_outcomes.values()
        ),
        files_degraded=sum(
            status == "degraded" for status in file_outcomes.values()
        ),
        files_needs_review=sum(
            status == "needs_review" for status in file_outcomes.values()
        ),
        review_reasons=dict(stats.get("review_reasons", {})),
        file_outcomes=file_outcomes,
        worker_queue_timed_out=teardown.queue_timed_out,
        lingering_workers=teardown.lingering_workers,
    )


def _teardown_worker(
    ctx: IngestContext,
    *,
    upsert_stop_event: threading.Event,
    upsert_queue: Queue[UpsertQueueItem | None],
    upsert_threads: list[threading.Thread],
) -> WorkerTeardownReport:
    """
    Tear down the upsert worker thread.

    Two paths:
    - Normal: drain queue gracefully, then stop worker
    - Abort: drain and fail all pending work, then stop worker

    NOTE: Coordinator must be closed BEFORE calling this function.
    """
    join_timeout = getattr(
        ctx.config,
        "upsert_join_timeout_seconds",
        INGEST_UPSERT_JOIN_TIMEOUT_SECONDS,
    )
    poll_seconds = getattr(
        ctx.config,
        "upsert_join_poll_seconds",
        INGEST_UPSERT_JOIN_POLL_SECONDS,
    )

    if upsert_stop_event.is_set():
        # Abort path: drain queue and mark all pending batches as failed
        ctx.logger.warning(
            "Upsert worker stop_event set; draining and failing pending batches"
        )
        pending_count = 0
        while True:
            try:
                queued = upsert_queue.get_nowait()
                if queued is None:
                    # Sentinel already queued (unlikely), put it back
                    upsert_queue.put(None)
                    break
                _mark_batch_failed(ctx, queued.batch, reason="worker_aborted")
                upsert_queue.task_done()
                pending_count += 1
            except Empty:
                break

        if pending_count > 0:
            ctx.logger.warning(
                "Marked %d pending batch(es) as failed during abort", pending_count
            )

        # Send sentinel and wait for worker to exit
        for _ in upsert_threads:
            upsert_queue.put(None)
        for thread in upsert_threads:
            thread.join(timeout=join_timeout)
        lingering = tuple(thread.name for thread in upsert_threads if thread.is_alive())
        if lingering:
            ctx.stats.increment("lingering_workers_total", len(lingering))
        return WorkerTeardownReport(
            aborted=True,
            lingering_workers=lingering,
        )

    else:
        # Normal path: wait for all work to complete, then stop worker
        ctx.logger.info("Waiting for upsert queue to drain...")
        queue_join_start = time.perf_counter()

        # Poll join with timeout to allow logging
        queue_drained = False
        elapsed = 0.0
        next_log_at = 10.0
        # Rollout fault-injection seam (no-op unless armed in a non-prod env). An
        # armed fault skips the wait loop so the queue is treated as not drained,
        # exercising the queue-drain-timeout handling below (VECTOR-09).
        drain_fault_injected = False
        try:
            maybe_fail(FaultPoint.QUEUE_DRAIN)
        except Exception:  # pylint: disable=broad-except
            ctx.logger.warning(
                "Injected queue-drain fault; treating queue as not drained"
            )
            drain_fault_injected = True
        while not drain_fault_injected and elapsed < join_timeout:
            try:
                # Try to join with a short timeout so we can log progress
                remaining = join_timeout - elapsed
                poll_timeout = min(poll_seconds, remaining)

                # Note: queue.join() doesn't take timeout in Python, so we check size
                if upsert_queue.unfinished_tasks == 0:
                    queue_drained = True
                    break

                time.sleep(poll_timeout)
                elapsed = time.perf_counter() - queue_join_start

                # Log every 10s after first 10s
                if elapsed >= next_log_at:
                    next_log_at = elapsed + 10.0
                    ctx.logger.info(
                        "Still waiting for upsert queue (%.1fs elapsed, %d tasks remaining)...",
                        elapsed,
                        upsert_queue.unfinished_tasks,
                    )
            except KeyboardInterrupt:
                ctx.logger.warning("Keyboard interrupt during queue drain; aborting")
                upsert_stop_event.set()
                # Switch to abort path (don't recurse, handle inline)
                while True:
                    try:
                        queued = upsert_queue.get_nowait()
                        if queued is None:
                            upsert_queue.put(None)
                            break
                        _mark_batch_failed(
                            ctx, queued.batch, reason="keyboard_interrupt"
                        )
                        upsert_queue.task_done()
                    except Empty:
                        break
                for _ in upsert_threads:
                    upsert_queue.put(None)
                for thread in upsert_threads:
                    thread.join(timeout=join_timeout)
                lingering = tuple(
                    thread.name for thread in upsert_threads if thread.is_alive()
                )
                ctx.stats.increment("ingest_cancelled_total")
                return WorkerTeardownReport(
                    cancelled=True,
                    lingering_workers=lingering,
                )

        if not queue_drained:
            ctx.stats.increment("worker_queue_timeout_total")
            ctx.logger.warning(
                "Upsert queue join timed out after %.1fs (%d tasks remaining); sending sentinel anyway",
                join_timeout,
                upsert_queue.unfinished_tasks,
            )
            ctx.logger.warning(
                "Upsert queue timeout details: unfinished_tasks=%d",
                upsert_queue.unfinished_tasks,
            )
            upsert_stop_event.set()
            active_batches = getattr(ctx, "active_upsert_batches", None)
            active_lock = getattr(ctx, "active_upsert_batches_lock", None)
            if active_batches is not None and active_lock is not None:
                with active_lock:
                    running_batches = list(active_batches.values())
                for running_batch in running_batches:
                    _mark_batch_failed(
                        ctx, running_batch, reason="worker_execution_timeout"
                    )
            drained = 0
            failed_files: set[str] = set()
            while True:
                try:
                    queued = upsert_queue.get_nowait()
                    if queued is None:
                        # Sentinel already queued (unlikely), put it back
                        upsert_queue.put(None)
                        break
                    batch = queued.batch
                    _mark_batch_failed(ctx, batch, reason="queue_drain_timeout")
                    for vector in batch:
                        metadata = vector.get("metadata") or {}
                        file_key = metadata.get("key") or metadata.get("source")
                        if isinstance(file_key, str) and file_key:
                            failed_files.add(file_key)
                            continue
                        vector_id = vector.get("id")
                        if isinstance(vector_id, str) and vector_id:
                            file_id = vector_id.split(":", 1)[0]
                            if file_id:
                                failed_files.add(file_id)
                    upsert_queue.task_done()
                    drained += 1
                except Empty:
                    break
            if failed_files:
                ctx.stats.increment("files_failed", len(failed_files))
                for failed_file in sorted(failed_files):
                    ctx.stats.append_failed(failed_file)
                preview = ", ".join(sorted(failed_files)[:10])
                ctx.logger.warning(
                    "Upsert queue timeout pending files (showing up to 10 of %d): %s",
                    len(failed_files),
                    preview,
                )
            if drained > 0:
                ctx.logger.warning(
                    "Drained and failed %d pending batch(es) after queue timeout",
                    drained,
                )
        else:
            queue_join_elapsed = time.perf_counter() - queue_join_start
            ctx.logger.info("Upsert queue drained in %.3fs", queue_join_elapsed)

        # Send sentinel to stop worker
        for _ in upsert_threads:
            upsert_queue.put(None)

        # Wait for thread to exit
        ctx.logger.info("Waiting for upsert worker thread to exit...")
        thread_join_start = time.perf_counter()
        for thread in upsert_threads:
            thread.join(timeout=join_timeout)
        thread_join_elapsed = time.perf_counter() - thread_join_start

        still_alive = [t.name for t in upsert_threads if t.is_alive()]
        if still_alive:
            ctx.stats.increment("lingering_workers_total", len(still_alive))
            ctx.logger.error(
                "Upsert worker thread(s) did not exit after %.1fs: %s",
                join_timeout,
                ", ".join(still_alive),
            )
        else:
            ctx.logger.info(
                "Upsert worker thread(s) exited in %.3fs", thread_join_elapsed
            )
        return WorkerTeardownReport(
            queue_timed_out=not queue_drained,
            lingering_workers=tuple(still_alive),
        )


# ---------------------------------------------------------------------------
# 8. ingest_local_directory_with_progress  (thin UI/progress wrapper)
# ---------------------------------------------------------------------------


def _emit_ingest_metrics(
    ctx: IngestContext,
    report: IngestReport,
    base_path: str,
) -> None:
    """Export Prometheus and event-sink metrics after ingestion."""
    prom_path = (getattr(ctx.config, "prometheus_metrics_file", "") or "").strip()
    if prom_path:
        try:
            ctx.event_sink.export_metrics(
                stats=ctx.stats,
                output_path=prom_path,
                duration_seconds=report.duration_seconds,
                vectors_per_second=report.vectors_per_second,
                source_path=base_path,
                dry_run=bool(getattr(ctx.config, "dry_run", False)),
                upsert_workers=ctx.config.upsert_workers,
            )
            ctx.logger.info("Exported Prometheus metrics to %s", prom_path)
        except IngestError as error:
            ctx.logger.warning("Could not export Prometheus metrics: %s", error)

    if getattr(ctx.config, "export_events", False):
        metrics = {
            "event_type": "ingestion_summary",
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "source_path": base_path,
            "dry_run": bool(getattr(ctx.config, "dry_run", False)),
            "duration_seconds": report.duration_seconds,
            "files_processed": report.files_processed,
            "vectors_created": report.total_vectors,
            "vectors_per_second": report.vectors_per_second,
            "failures": report.files_failed,
            "terminal_status": report.status.value,
        }
        try:
            ctx.event_sink.emit_event(metrics)
        except IngestError as error:
            ctx.logger.warning("Could not write ingestion summary event: %s", error)


def _log_ingest_summary(ctx: IngestContext, report: IngestReport) -> None:
    ctx.logger.info(
        """========================================
            INGESTION SUMMARY
            ========================================
            Files found:          %d
            Files processed:      %d
            Files skipped:        %d
            Files failed:         %d
            Files degraded:       %d
            Files needs review:   %d
            Total vectors:        %d
            Duration:             %.2fs
            Avg speed:            %.1f vectors/sec
            Terminal status:      %s
            ========================================
            """,
        report.files_found,
        report.files_processed,
        report.files_skipped,
        report.files_failed,
        report.files_degraded,
        report.files_needs_review,
        report.total_vectors,
        report.duration_seconds,
        report.vectors_per_second,
        report.status.value,
    )
    if report.failed_files:
        ctx.logger.warning("Failed files:")
        for failed_file in report.failed_files:
            ctx.logger.warning("  - %s", failed_file)
    if report.review_reasons:
        ctx.logger.info("Files needing review by reason:")
        for reason, count in sorted(report.review_reasons.items()):
            ctx.logger.info("  - %s: %d", reason, count)
    ctx.logger.info("Ingestion finished with status=%s", report.status.value)


def ingest_local_directory_with_progress(
    ctx: IngestContext,
    use_progress_bar: bool = True,
) -> IngestReport:
    """
    Enhanced ingestion with progress tracking and security.

    This is a thin UI/progress wrapper around run_ingest().
    All core logic lives in run_ingest() and can be exercised independently.
    """
    base_path = ctx.config.local_path

    # Validate directory safety to prevent path traversal
    try:
        validated_path = validate_directory_safety(base_path)
        base_path = str(validated_path)
        ctx.logger.info("Validated ingest directory: %s", validated_path)
    except FileOperationSecurityError as e:
        ctx.logger.error("Invalid ingest directory: %s", e)
        raise FileNotFoundError(f"Invalid directory: {base_path}") from e

    objs = list_local_files_secure(
        base_path,
        ctx.config.ext_whitelist,
        ctx.config.max_file_mb,
        logger=ctx.logger,
    )
    objs = [
        obj
        for obj in objs
        if Path(str(obj.get("Key", ""))).name.lower() != "resolved_buildings.csv"
    ]
    ctx.logger.info("Found %d files to process in %s", len(objs), base_path)

    if not objs:
        ctx.logger.warning("No files found to process")
        report = IngestReport(
            files_found=0,
            files_processed=0,
            files_skipped=0,
            files_failed=0,
            total_vectors=0,
            duration_seconds=0.0,
            status=IngestTerminalStatus.EMPTY_INPUT,
        )
        get_telemetry().record_ingest_outcome("run", report.status)
        _emit_ingest_metrics(ctx, report, base_path)
        _log_ingest_summary(ctx, report)
        return report

    # Building resolution
    name_to_canonical: dict[str, str] = {}
    alias_to_canonical: dict[str, str] = {}
    known_buildings: list[str] = []
    csv_candidates = [
        obj["Key"]
        for obj in objs
        if "Property" in obj["Key"]
        and obj["Key"].endswith(".csv")
        and "maintenance" not in obj["Key"].lower()
    ]
    if csv_candidates:
        known_buildings, name_to_canonical, alias_to_canonical = (
            load_building_names_with_aliases(ctx, base_path, csv_candidates[0])
        )
    else:
        ctx.logger.warning("No property CSV found for building name resolution")

    manual_overrides = load_manual_building_overrides(
        Path(base_path) / "resolved_buildings.csv"
    )
    if manual_overrides:
        ctx.logger.info(
            "Loaded %d manual building override(s) from resolved_buildings.csv",
            len(manual_overrides),
        )

    building_resolver = FilenameBuildingResolver(
        name_to_canonical=name_to_canonical,
        alias_to_canonical=alias_to_canonical,
        known_buildings=known_buildings,
        manual_overrides=manual_overrides,
    )

    processor = DocumentProcessor(
        ctx=ctx,
        base_path=base_path,
        alias_to_canonical=alias_to_canonical,
        fra_vector_extractor=extract_fra_risk_items_integration,
        building_resolver=building_resolver,
    )
    orchestrator = FileIngestOrchestrator(processor)

    use_worker = (
        not getattr(ctx.config, "dry_run", False)
        and ctx.config.upsert_strategy == "worker"
    )
    upsert_queue: Queue[UpsertQueueItem | None] | None = None
    upsert_errors: list[Exception] = []
    upsert_stop_event = threading.Event()
    ctx.upsert_stop_event = upsert_stop_event

    worker_pool: ProcessPoolExecutor | None = None
    try:
        if ctx.config.max_parse_workers > 1:
            # One pool shared by extraction/chunking and FRA parsing; both are
            # CPU-bound and share the same worker budget.
            worker_pool = ProcessPoolExecutor(max_workers=ctx.config.max_parse_workers)
            orchestrator.set_cpu_pool(worker_pool)
            orchestrator.set_parse_pool(worker_pool)

        upsert_threads: list[threading.Thread] | None = None

        if use_worker:
            batch_size = max(
                1,
                min(
                    int(ctx.config.upsert_batch),
                    max(1, int(ctx.config.max_pending_vectors)),
                ),
            )
            queue_max = max(1, int(ctx.config.max_pending_vectors) // batch_size)
            upsert_queue = Queue(maxsize=queue_max)
            upsert_threads = []
            worker_count = max(1, int(ctx.config.upsert_workers))
            for idx in range(worker_count):
                thread = threading.Thread(
                    target=_upsert_worker,
                    name=f"upsert-worker-{idx + 1}",
                    args=(ctx, upsert_queue, upsert_stop_event, upsert_errors),
                    daemon=True,
                )
                thread.start()
                upsert_threads.append(thread)

        writer = Writer(ctx, upsert_vectors_atomic)
        coordinator = VectorWriteCoordinator(
            ctx,
            writer=writer,
            use_worker=use_worker,
            upsert_queue=upsert_queue,
            stop_event=upsert_stop_event,
            max_pending_vectors=ctx.config.max_pending_vectors,
            upsert_batch=ctx.config.upsert_batch,
            flush_seconds=getattr(ctx.config, "upsert_flush_seconds", 0),
        )

        with IngestionProgressTracker(
            len(objs),
            use_tqdm=use_progress_bar,
            progress_log_interval=ctx.config.progress_log_interval,
            logger=ctx.logger,
        ) as progress:
            report = run_ingest(
                ctx,
                objs,
                orchestrator=orchestrator,
                coordinator=coordinator,
                upsert_stop_event=upsert_stop_event,
                upsert_queue=upsert_queue,
                upsert_threads=upsert_threads,
                upsert_errors=upsert_errors,
                use_worker=use_worker,
                base_path=base_path,
                progress=progress,
            )

    finally:
        ctx.upsert_stop_event = None
        if worker_pool is not None:
            worker_pool.shutdown(wait=True)

    _emit_ingest_metrics(ctx, report, base_path)
    _log_ingest_summary(ctx, report)
    return report
