"""
Transaction management for Alfred Local ingestion.
This module provides classes and functions to handle transactional operations during the ingestion process,
including thread-safe statistics tracking, retry mechanisms, and FRA-specific processing.
"""

import logging
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import Lock, RLock
from typing import TYPE_CHECKING, Any, Optional

from building.normaliser import normalise_building_name
from config import (
    FRA_LOCK_TIMEOUT_SECONDS,
    FRA_RISK_ITEMS_NAMESPACE,
    INGEST_VECTOR_BUFFER_MAX_SIZE,
    INGEST_VERIFY_ATTEMPTS,
    INGEST_VERIFY_BACKOFF_BASE,
    INGEST_VERIFY_BACKOFF_CAP,
    INGEST_VERIFY_FETCH_BATCH_SIZE,
    DocumentTypes,
    get_display_namespace,
)
from config.constant import INGEST_METADATA_CACHE_SIZE
from core.alfred_exceptions import (
    CriticalInconsistentError,
    ExternalServiceError,
    IngestError,
    RollbackError,
)
from core.fault_injection import FaultPoint, maybe_fail
from core.pinecone_utils import NULL_SENTINEL
from core.telemetry import get_telemetry
from fra import (
    EnrichedRiskItem,
    FRAActionPlanParser,
    FRATriageComputer,
    FraVectorExtractResult,
    _fra_partition_key,
    deduplicate_risk_items,
    mark_superseded_risk_items,
    parse_action_plan_in_process,
    restore_superseded_items,
    sanitise_risk_item_for_metadata,
)
from interfaces import (
    FileRecord,
    FraJournalState,
    JobRecord,
    MetricsReader,
    new_fra_journal_record,
)

from .document_content import (
    backoff_sleep,
    embed_texts_batch,
    ext,
)
from .helpers import _extract_fra_layout_text
from .observability import emit_event_safely
from .registry_reconciliation import spool_registry_divergence
from .utils import (
    upsert_vectors,
    validate_with_truncation,
)

if TYPE_CHECKING:
    from .context import IngestContext

# ============================================================================
# THREAD-SAFE STATS
# ============================================================================


class ThreadSafeStats(MetricsReader):
    def __init__(self):
        self._lock = Lock()
        self._stats = {
            "run_id": uuid.uuid4().hex,
            "files_processed": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "total_vectors": 0,
            "vectors_skipped": 0,
            "failed_files": [],
            "file_terminal_states": {},
            "review_reasons": {},
        }

    def increment(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._stats[key] = self._stats.get(key, 0) + amount

    def append_failed(self, filename: str) -> None:
        with self._lock:
            self._stats["failed_files"].append(filename)

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self._stats.copy()
            snapshot["failed_files"] = list(self._stats["failed_files"])
            snapshot["file_terminal_states"] = dict(
                self._stats["file_terminal_states"]
            )
            snapshot["review_reasons"] = dict(self._stats["review_reasons"])
            return snapshot

    def record_file_terminal(
        self, file_key: str, status: str, reason: str | None = None
    ) -> None:
        """Record one terminal state without double-counting repeated writes.

        ``reason`` is the low-cardinality review reason (INGEST-08) recorded when
        ``status`` is ``needs_review``; it is counted only when the terminal
        state is newly set, so repeated writes cannot inflate the tally.
        """

        if not file_key:
            return
        with self._lock:
            states = self._stats["file_terminal_states"]
            previous = states.get(file_key)
            if previous == status:
                return
            # Success must never promote an already incomplete/failed/degraded
            # file: a lossy encoding fallback (degraded) is a real signal that
            # a later clean-looking success in the same run must not erase.
            if previous in {
                "partial",
                "failed",
                "critical_inconsistent",
                "degraded",
            } and status in {"success", "success_with_skips"}:
                return
            # A milder degraded outcome must never overwrite a worse
            # partial/failed/critical one.
            if (
                previous in {"partial", "failed", "critical_inconsistent"}
                and status == "degraded"
            ):
                return
            if previous == "critical_inconsistent":
                return
            states[file_key] = status
            record_review = status == "needs_review" and bool(reason)
            if record_review:
                reasons = self._stats["review_reasons"]
                reasons[reason] = reasons.get(reason, 0) + 1
        get_telemetry().record_ingest_outcome("file", status)
        if record_review:
            get_telemetry().record_ingest_review(str(reason))

    def observe_timing(self, key: str, value: float) -> None:
        with self._lock:
            count_key = f"{key}_count"
            sum_key = f"{key}_sum"
            max_key = f"{key}_max"
            self._stats[count_key] = self._stats.get(count_key, 0) + 1
            self._stats[sum_key] = self._stats.get(sum_key, 0.0) + float(value)
            current_max = self._stats.get(max_key, 0.0)
            if float(value) > float(current_max):
                self._stats[max_key] = float(value)

    def observe_histogram(self, key: str, value: float) -> None:
        """Record a value for a histogram metric (e.g. metadata size)."""
        with self._lock:
            count_key = f"{key}_count"
            sum_key = f"{key}_sum"
            max_key = f"{key}_max"
            self._stats[count_key] = self._stats.get(count_key, 0) + 1
            self._stats[sum_key] = self._stats.get(sum_key, 0.0) + float(value)
            current_max = self._stats.get(max_key, 0.0)
            if float(value) > float(current_max):
                self._stats[max_key] = float(value)


class FileCompletionTracker:
    """Track vector completion so files are only marked successful once."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._expected: dict[str, set[str]] = {}
        self._succeeded: dict[str, set[str]] = {}
        self._templates: dict[str, dict[str, Any]] = {}
        self._namespaces: dict[str, set[str | None]] = {}
        self._failed: set[str] = set()
        self._finalised: set[str] = set()

    @staticmethod
    def _file_id(vector: dict[str, Any]) -> str | None:
        explicit_file_id = vector.get("_file_id")
        if explicit_file_id:
            return str(explicit_file_id)
        vector_id = vector.get("id")
        if not vector_id:
            return None
        return str(vector_id).split(":", 1)[0] or None

    def register(self, vectors: list[dict[str, Any]]) -> None:
        """Register a file's full vector set before any batches are queued."""
        with self._lock:
            for vector in vectors:
                file_id = self._file_id(vector)
                vector_id = vector.get("id")
                if file_id is None or not vector_id:
                    continue
                self._expected.setdefault(file_id, set()).add(str(vector_id))
                self._succeeded.setdefault(file_id, set())
                self._templates.setdefault(file_id, vector)
                self._namespaces.setdefault(file_id, set()).add(vector.get("namespace"))

    def record_success(self, batch: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Return one synthetic batch for each file completed by this batch."""
        completed: list[list[dict[str, Any]]] = []
        with self._lock:
            touched: set[str] = set()
            for vector in batch:
                file_id = self._file_id(vector)
                vector_id = vector.get("id")
                if file_id is None or not vector_id:
                    continue
                if file_id in self._failed or file_id in self._finalised:
                    continue
                # Direct callers may not have registered expected IDs.
                self._expected.setdefault(file_id, set()).add(str(vector_id))
                self._succeeded.setdefault(file_id, set()).add(str(vector_id))
                self._templates.setdefault(file_id, vector)
                self._namespaces.setdefault(file_id, set()).add(vector.get("namespace"))
                touched.add(file_id)

            for file_id in touched:
                if not self._expected[file_id].issubset(self._succeeded[file_id]):
                    continue
                template = self._templates[file_id]
                namespaces = self._namespaces.get(file_id) or {None}
                completed.append(
                    [
                        {
                            "id": template["id"],
                            "metadata": template.get("metadata") or {},
                            "namespace": namespace,
                            "_file_id": file_id,
                            "_processing_token": template.get("_processing_token"),
                            "_file_partial": template.get("_file_partial", False),
                            "_file_partial_reason": template.get(
                                "_file_partial_reason"
                            ),
                            "_file_terminal_status": template.get(
                                "_file_terminal_status"
                            ),
                            "_file_terminal_reason": template.get(
                                "_file_terminal_reason"
                            ),
                        }
                        for namespace in namespaces
                    ]
                )
                self._finalised.add(file_id)
                self._expected.pop(file_id, None)
                self._succeeded.pop(file_id, None)
                self._templates.pop(file_id, None)
                self._namespaces.pop(file_id, None)
        return completed

    def record_failure(self, batch: list[dict[str, Any]]) -> dict[str, str]:
        """Return ``partial`` when earlier vectors for the file succeeded."""

        outcomes: dict[str, str] = {}
        with self._lock:
            for vector in batch:
                file_id = self._file_id(vector)
                if file_id is not None:
                    outcomes[file_id] = (
                        "partial" if self._succeeded.get(file_id) else "failed"
                    )
                    self._failed.add(file_id)
        return outcomes


# ============================================================================
# THREAD-SAFE CACHE
# ============================================================================


class ThreadSafeCache:
    def __init__(self, metadata_cache_size: int = INGEST_METADATA_CACHE_SIZE):
        self._lock = RLock()
        self._name_cache: dict[str, str] = {}
        self._alias_cache: dict[str, str] = {}
        self._metadata_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._metadata_cache_size = metadata_cache_size
        self._embedding_cache: OrderedDict[str, list[float]] = OrderedDict()

    def update_from_csv(
        self,
        name_to_canonical: dict[str, str],
        alias_to_canonical: dict[str, str],
        metadata_cache: dict[str, dict[str, Any]],
    ) -> None:
        with self._lock:
            self._name_cache.update(name_to_canonical)
            self._alias_cache.update(alias_to_canonical)
            for k, v in metadata_cache.items():
                self._metadata_cache[k] = v.copy()
                self._metadata_cache.move_to_end(k)
                if len(self._metadata_cache) > self._metadata_cache_size:
                    self._metadata_cache.popitem(last=False)

    def get_name_mapping(self) -> dict[str, str]:
        with self._lock:
            return self._name_cache.copy()

    def get_alias_mapping(self) -> dict[str, str]:
        with self._lock:
            return self._alias_cache.copy()

    def get_metadata(self, building_name: str) -> Optional[dict[str, Any]]:
        """
        Get cached metadata for a building.
        Fixed: Added missing implementation.

        Args:
            building_name: The canonical building name

        Returns:
            Dictionary of cached metadata or None if not found
        """
        with self._lock:
            metadata = self._metadata_cache.get(building_name)
            if metadata is not None:
                # LRU: mark most recently used
                self._metadata_cache.move_to_end(building_name)
            # Return a copy to prevent external modifications
            return metadata.copy() if metadata else None

    def set_metadata(self, building_name: str, metadata: dict[str, Any]) -> None:
        """
        Cache metadata for a building.

        Args:
            building_name: The canonical building name
            metadata: Metadata dictionary to cache
        """
        with self._lock:
            self._metadata_cache[building_name] = metadata.copy()
            self._metadata_cache.move_to_end(building_name)
            if len(self._metadata_cache) > self._metadata_cache_size:
                self._metadata_cache.popitem(last=False)

    def has_metadata(self, building_name: str) -> bool:
        """Check if metadata exists for a building."""
        with self._lock:
            return building_name in self._metadata_cache

    def invalidate_building(self, building: str) -> None:
        with self._lock:
            self._name_cache.pop(building, None)
            self._alias_cache.pop(building, None)
            self._metadata_cache.pop(building, None)
            self._embedding_cache.pop(building, None)

    def clear_all(self) -> None:
        with self._lock:
            self._name_cache.clear()
            self._alias_cache.clear()
            self._metadata_cache.clear()
            self._embedding_cache.clear()

    def get_embedding(self, building_name: str) -> list[float] | None:
        with self._lock:
            embedding = self._embedding_cache.get(building_name)
            if embedding is not None:
                self._embedding_cache.move_to_end(building_name)
                return list(embedding)
            return None

    def set_embedding(self, building_name: str, embedding: list[float]) -> None:
        with self._lock:
            self._embedding_cache[building_name] = list(embedding)
            self._embedding_cache.move_to_end(building_name)
            if len(self._embedding_cache) > self._metadata_cache_size:
                self._embedding_cache.popitem(last=False)


# ============================================================================
# THREAD-SAFE VECTOR BUFFER
# ============================================================================


class ThreadSafeVectorBuffer:
    """Thread-safe buffer for pending vectors."""

    def __init__(self, max_size: int = INGEST_VECTOR_BUFFER_MAX_SIZE):
        self._lock = RLock()
        self._buffer: list[dict[str, Any]] = []
        self.max_size = max_size

    def add(self, vector: dict[str, Any], auto_flush_callback=None) -> bool:
        """
        Add vector with optional auto-flush.

        Returns:
            True if added, False if buffer full (and auto_flush not provided)
        """
        with self._lock:
            if len(self._buffer) >= self.max_size:
                if auto_flush_callback:
                    # Auto-flush when full
                    to_flush = self._buffer[:]
                    self._buffer.clear()
                    auto_flush_callback(to_flush)
                    self._buffer.append(vector)
                    return True
                else:
                    return False  # Let caller decide how to handle
            self._buffer.append(vector)
            return True

    def get_and_clear(self) -> list[dict[str, Any]]:
        """Retrieve and clear all buffered vectors (used for upserts)."""
        with self._lock:
            data = self._buffer[:]
            self._buffer.clear()
            return data

    def extend(self, vectors: list[dict[str, Any]]) -> None:
        """Add multiple vectors to the buffer."""
        with self._lock:
            remaining_space = self.max_size - len(self._buffer)
            if len(vectors) > remaining_space:
                raise BufferError("Not enough space in buffer for extend()")
            self._buffer.extend(vectors)

    def size(self) -> int:
        """Return the current size of the buffer."""
        with self._lock:
            return len(self._buffer)

    def __len__(self) -> int:
        """Return the number of vectors in the buffer (len() support)."""
        with self._lock:
            return len(self._buffer)

    def is_empty(self) -> bool:
        """Return True if the buffer has no vectors."""
        with self._lock:
            return len(self._buffer) == 0


class FraSupersessionTxnLog:
    """Lightweight in-memory transaction log for supersession rollback tracking."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._tx_superseded: dict[str, list[str]] = {}
        self.logger = logger or logging.getLogger(__name__)

    def begin(self, buildings: list[str], request_count: int) -> str:
        tx_id = uuid.uuid4().hex
        self._tx_superseded[tx_id] = []
        self.logger.info(
            "FRA txn begin: %s (buildings=%d, requests=%d)",
            tx_id,
            len(buildings),
            request_count,
        )
        return tx_id

    def record_superseded(self, tx_id: str, building: str, ids: list[str]) -> None:
        if tx_id not in self._tx_superseded:
            return
        self._tx_superseded[tx_id].extend(ids)
        self.logger.info(
            "FRA txn record: %s (building=%s, superseded=%d)",
            tx_id,
            building,
            len(ids),
        )

    def get_superseded(self, tx_id: str) -> list[str]:
        return list(self._tx_superseded.get(tx_id, []))

    def commit(self, tx_id: str) -> None:
        if tx_id in self._tx_superseded:
            self.logger.info(
                "FRA txn commit: %s (superseded=%d)",
                tx_id,
                len(self._tx_superseded[tx_id]),
            )
            self._tx_superseded.pop(tx_id, None)

    def rollback(self, tx_id: str, ctx: "IngestContext", reason: str) -> int:
        """Rollback superseded items and return count restored."""
        if tx_id not in self._tx_superseded:
            return 0

        superseded_ids = self._tx_superseded[tx_id]
        self.logger.warning(
            "FRA txn rollback: %s (restoring %d items, reason=%s)",
            tx_id,
            len(superseded_ids),
            reason,
        )
        # Call the restore function from fra_integration
        restored = restore_superseded_items(ctx, superseded_ids)
        self._tx_superseded.pop(tx_id, None)  # Clear the log after rollback
        if superseded_ids and restored < len(superseded_ids):
            raise RollbackError(
                f"Critical rollback failure: restored {restored}/{len(superseded_ids)} superseded items "
                f"(tx_id={tx_id}, reason={reason})"
            )
        return restored


# ---------------------------------------------------------------------------
# 6. FraTransaction  (explicit prepare / execute / verify / rollback)
# ---------------------------------------------------------------------------


class FraTransaction:
    """
    Encapsulates the phases of an FRA atomic upsert:

      collect_supersession_requests() — identify candidate buildings
      acquire_locks() — serialise the complete transaction lifecycle
      prepare()  — re-check idempotency, journal, and block while locked
      execute()  — mark superseded items, upsert vectors
      verify()   — confirm vectors are present in the index
      rollback() — restore superseded items on failure

    Locks are held from before prepare() through commit or rollback.
    """

    def __init__(self, ctx: "IngestContext", vectors: list[dict[str, Any]]) -> None:
        self._ctx = ctx
        self._vectors = vectors
        self._candidate_requests: list[tuple[str, str]] = []
        self._supersede_requests: list[tuple[str, str]] = []
        self._superseded_ids: list[str] = []
        self._tx_id: str | None = None
        self._lock_ctx = None
        # Lock ownership and run shutdown are different signals. Reusing the
        # run-wide stop event here made one worker failure look like every
        # active FRA transaction had lost its Redis locks, cascading a single
        # error into multiple rollback failures.
        self._lock_lost_event = threading.Event()

    # -- phase helpers --

    def collect_supersession_requests(self) -> bool:
        """
        Collect candidate supersession requests without consulting mutable
        transaction state. This phase is safe to run before locking.
        """
        self._candidate_requests = _collect_fra_supersede_requests(self._vectors)
        return bool(self._candidate_requests)

    def prepare(self) -> bool:
        """Prepare a transaction while holding all FRA locks.

        The idempotency lookup, durable-block check and journal/block writes
        must be inside the same lock scope. Otherwise a transaction waiting on
        the global lock can publish a block that another live worker mistakes
        for an abandoned transaction requiring reconciliation.

        Returns True if a transactional supersession is required. A False
        result means a preceding serial transaction already completed the
        building/date request and the vectors can use the simple upsert path.
        """
        self._raise_if_lock_lost()
        self._supersede_requests = _filter_supersede_requests_with_registry(
            self._ctx, self._candidate_requests
        )
        for building, _ in self._supersede_requests:
            blocking_tx = self._ctx.fra_journal.blocking_transaction(building)
            if blocking_tx:
                raise CriticalInconsistentError(
                    "FRA supersession is blocked pending reconciliation"
                )
        if not self._supersede_requests:
            return False

        buildings = self.buildings
        self._tx_id = uuid.uuid4().hex
        vector_ids = [
            str(vector["id"])
            for vector in self._vectors
            if vector.get("namespace") == FRA_RISK_ITEMS_NAMESPACE and vector.get("id")
        ]
        try:
            self._ctx.fra_journal.begin(
                new_fra_journal_record(
                    tx_id=self._tx_id,
                    buildings=buildings,
                    requests=self._supersede_requests,
                    vector_ids=vector_ids,
                )
            )
            # The durable block is written only after the distributed locks
            # are held. It survives a process crash without blocking sibling
            # batches that are merely waiting for the same live lock.
            self._ctx.fra_journal.block_buildings(self._tx_id, buildings)
        except Exception as error:  # pylint: disable=broad-except
            raise ExternalServiceError("FRA transaction journal unavailable") from error
        return True

    def acquire_locks(self):
        """
        Acquire Redis locks for all buildings involved.
        Returns the lock context manager (caller must enter it).
        """
        buildings = sorted({building for building, _ in self._candidate_requests})
        return _acquire_fra_locks(
            self._ctx,
            buildings,
            timeout_seconds=FRA_LOCK_TIMEOUT_SECONDS,
            lock_lost_event=self._lock_lost_event,
        )

    def execute(self) -> None:
        """Mark superseded items and upsert vectors. Must hold locks."""
        self._raise_if_lock_lost()
        self._ctx.logger.info(
            "FRA supersession batch: %d building/date pairs",
            len(self._supersede_requests),
        )
        for building, assessment_date in self._supersede_requests:
            self._raise_if_lock_lost()
            self._ctx.logger.info(
                "Superseding FRA items for %s (new assessment: %s)",
                building,
                assessment_date,
            )

        for building, assessment_date in self._supersede_requests:
            self._raise_if_lock_lost()
            superseded = mark_superseded_risk_items(
                ctx=self._ctx,
                building=building,
                new_assessment_date=assessment_date,
                before_update=self._journal_supersession_plan,
                finalise_job=False,
            )
            self._superseded_ids.extend(superseded)

        self._transition(FraJournalState.SUPERSEDED)

        self._raise_if_lock_lost()
        upsert_vectors(self._ctx, self._vectors)
        self._ctx.stats.increment("batch_state_upserted_total")
        self._transition(FraJournalState.UPSERTED)

    def verify(self) -> "FraVerificationOutcome":
        """
        Confirm all FRA vectors are present in the index.
        Returns a list of missing IDs (empty on success).
        Commits the txn log on success.
        """
        self._raise_if_lock_lost()
        outcome = _coerce_verification_outcome(
            _verify_fra_vectors_present(
                self._ctx, self._vectors, attempts=INGEST_VERIFY_ATTEMPTS
            )
        )
        if outcome.state is FraVerificationState.PRESENT:
            self._transition(FraJournalState.VERIFIED)
            self._finalise_jobs("success")
            self._transition(FraJournalState.COMMITTED)
            if self._tx_id:
                self._ctx.fra_journal.unblock_buildings(
                    self._tx_id, self.buildings
                )
        elif outcome.state is FraVerificationState.UNAVAILABLE:
            self._transition(
                FraJournalState.VERIFICATION_UNAVAILABLE,
                failure_code="vector.verification_unavailable",
            )
            if self._tx_id:
                self._ctx.fra_journal.block_buildings(
                    self._tx_id, self.buildings
                )
        return outcome

    def rollback(self, reason: str) -> None:
        """Restore and verify every planned supersession item."""

        if self._tx_id is None:
            return
        # Rollout fault-injection seam (no-op unless armed in a non-prod env).
        try:
            maybe_fail(FaultPoint.FRA_ROLLBACK)
        except Exception as error:  # pylint: disable=broad-except
            # An unavailable rollback mechanism means persisted state can no
            # longer be proven consistent. Enter the same fail-closed terminal
            # state as an incomplete restore instead of leaking a raw injected
            # dependency exception or leaving the transaction merely pending.
            record = self._ctx.fra_journal.get(self._tx_id)
            item_ids = list(record.superseded_ids) if record else self._superseded_ids
            try:
                self._mark_critical_inconsistent(
                    f"{reason}: rollback mechanism unavailable", item_ids, 0
                )
            except CriticalInconsistentError as critical:
                raise critical from error
        self._transition(
            FraJournalState.ROLLBACK_PENDING,
            failure_code="fra.supersession_failed",
        )
        record = self._ctx.fra_journal.get(self._tx_id)
        item_ids = list(record.superseded_ids) if record else self._superseded_ids
        restored = restore_superseded_items(self._ctx, item_ids)
        verified = _verify_restored_fra_items(self._ctx, item_ids)
        if restored != len(item_ids) or not verified:
            self._mark_critical_inconsistent(reason, item_ids, restored)
        self._finalise_jobs("failed")
        self._transition(FraJournalState.ROLLED_BACK)
        self._ctx.fra_journal.unblock_buildings(self._tx_id, self.buildings)
        self._ctx.stats.increment("fra_rollbacks_total")

    @property
    def buildings(self) -> list[str]:
        return sorted({building for building, _ in self._supersede_requests})

    def _journal_supersession_plan(self, item_ids: list[str]) -> None:
        if self._tx_id is None:
            raise ExternalServiceError("FRA transaction journal was not initialised")
        self._ctx.fra_journal.append_superseded(self._tx_id, item_ids)

    def _transition(
        self,
        state: FraJournalState,
        *,
        failure_code: str | None = None,
    ) -> None:
        if self._tx_id is None:
            raise ExternalServiceError("FRA transaction journal was not initialised")
        self._ctx.fra_journal.transition(
            self._tx_id, state, failure_code=failure_code
        )

    def _finalise_jobs(self, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat() + "Z"
        for building, assessment_date in self._supersede_requests:
            registry_id = (
                f"fra_supersede:{normalise_building_name(building)}:{assessment_date}"
            )
            self._ctx.job_registry.upsert(
                JobRecord(
                    job_id=registry_id,
                    job_type="fra_supersession",
                    status=status,
                    started_at_iso=now,
                    finished_at_iso=now,
                    error=None if status == "success" else "transaction_rolled_back",
                    meta={
                        "building": building,
                        "assessment_date": assessment_date,
                        "tx_id": self._tx_id,
                    },
                )
            )

    def _mark_critical_inconsistent(
        self, reason: str, item_ids: list[str], restored: int
    ) -> None:
        if self._tx_id is None:
            raise CriticalInconsistentError("FRA rollback could not be verified")
        try:
            self._ctx.fra_journal.transition(
                self._tx_id,
                FraJournalState.CRITICAL_INCONSISTENT,
                failure_code="fra.rollback_failed",
            )
            self._ctx.fra_journal.block_buildings(self._tx_id, self.buildings)
        finally:
            self._ctx.stats.increment("critical_inconsistent_total")
            self._ctx.stats.increment("rollback_failures_total")
            get_telemetry().record_ingest_integrity(
                "rollback", "critical_inconsistent"
            )
            emit_event_safely(
                self._ctx,
                {
                    "event_type": "fra_critical_inconsistent",
                    "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                    "transaction_id": self._tx_id,
                    "affected_count": len(item_ids),
                    "restored_count": restored,
                    "buildings": len(self.buildings),
                },
                description="Critical FRA alert event export",
            )
        raise CriticalInconsistentError(
            f"FRA rollback incomplete for transaction {self._tx_id}: {reason}"
        )

    # -- private --

    def _raise_if_lock_lost(self) -> None:
        if self._lock_lost_event.is_set():
            raise ExternalServiceError("Redis lock lost during FRA supersession")


# ---------------------------------------------------------------------------
# upsert_vectors_atomic  (refactored — delegates to FraTransaction)
# ---------------------------------------------------------------------------


def _emit_verification_failure_event(
    ctx: "IngestContext",
    missing_ids: list[str],
) -> None:
    emit_event_safely(
        ctx,
        {
            "event_type": "fra_verification_failed",
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "missing_count": len(missing_ids),
            "missing_ids": missing_ids[:50],
            "namespace": FRA_RISK_ITEMS_NAMESPACE,
        },
        description="Verification failure alert event export",
    )


def _handle_verification_failure(
    ctx: "IngestContext",
    vectors: list[dict[str, Any]],
    missing_ids: list[str],
) -> None:
    _emit_verification_failure_event(ctx, missing_ids)
    error_tag = f"verification_failed_missing_{len(missing_ids)}"
    ctx.stats.increment("batch_state_failed_total")
    try:
        _record_ingested_files(ctx, vectors, status="failed", error=error_tag)
    except Exception as record_error:  # pylint: disable=broad-except
        ctx.logger.warning("FileRegistry update failed: %s", record_error)
    raise ExternalServiceError(
        f"FRA upsert verification failed; missing {len(missing_ids)} vectors"
    )


def _record_registry_divergence(
    ctx: "IngestContext", vectors: list[dict[str, Any]]
) -> None:
    """Make vector-success/registry-failure visible at run level."""

    ctx.stats.increment("registry_divergence_total")
    get_telemetry().record_ingest_integrity("registry", "diverged")
    ctx.logger.error(
        "Vector write succeeded but file registry update failed; reconciliation required"
    )
    try:
        spooled = spool_registry_divergence(ctx, vectors)
        ctx.stats.increment("registry_reconciliation_spooled_total", spooled)
    except Exception as spool_error:  # pylint: disable=broad-except
        ctx.stats.increment("registry_reconciliation_spool_failures_total")
        ctx.logger.error("Registry reconciliation artifact failed: %s", spool_error)
    emit_event_safely(
        ctx,
        {
            "event_type": "ingest_registry_diverged",
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "affected_vectors": len(vectors),
        },
        description="Registry divergence alert event export",
    )


def _record_verification_unavailable(
    ctx: "IngestContext", vectors: list[dict[str, Any]]
) -> None:
    ctx.stats.increment("verification_unavailable_total")
    try:
        _record_ingested_files(
            ctx,
            vectors,
            status="partial",
            error="verification_read_unavailable",
        )
    except Exception:  # pylint: disable=broad-except
        _record_registry_divergence(ctx, vectors)


def upsert_vectors_atomic(ctx: "IngestContext", vectors: list[dict[str, Any]]) -> None:
    """
    Two-phase commit for FRA risk items:
      1. Prepare — collect supersession requests
      2. Execute — mark superseded items + upsert
      3. Verify  — confirm vectors present
      4. Rollback on any failure
    """
    if not vectors:
        return

    def simple_upsert_and_verify() -> None:
        # Simple path: no supersession needed
        upsert_vectors(ctx, vectors)
        ctx.stats.increment("batch_state_upserted_total")
        verification = _coerce_verification_outcome(
            _verify_fra_vectors_present(
                ctx, vectors, attempts=INGEST_VERIFY_ATTEMPTS
            )
        )
        if verification.state is FraVerificationState.UNAVAILABLE:
            _record_verification_unavailable(ctx, vectors)
            return
        if verification.state is FraVerificationState.MISSING:
            _handle_verification_failure(
                ctx, vectors, list(verification.missing_ids)
            )
        ctx.stats.increment("batch_state_verified_total")
        try:
            _record_ingested_files(ctx, vectors, status="success")
        except Exception as error:  # pylint: disable=broad-except
            ctx.logger.warning("FileRegistry update failed: %s", error)
            _record_registry_divergence(ctx, vectors)

    txn = FraTransaction(ctx, vectors)
    has_candidates = txn.collect_supersession_requests()

    if not has_candidates:
        simple_upsert_and_verify()
        return

    # Lock before consulting or publishing durable transaction blockers. The
    # global lock (when configured) therefore serialises the complete mutable
    # FRA lifecycle rather than only execute/verify.
    with txn.acquire_locks():
        needs_supersession = txn.prepare()
        if not needs_supersession:
            simple_upsert_and_verify()
            return
        try:
            txn.execute()
            verification = txn.verify()
            if verification.state is FraVerificationState.UNAVAILABLE:
                _record_verification_unavailable(ctx, vectors)
                return
            if verification.state is FraVerificationState.MISSING:
                _handle_verification_failure(
                    ctx, vectors, list(verification.missing_ids)
                )

            ctx.stats.increment("batch_state_verified_total")
            try:
                _record_ingested_files(ctx, vectors, status="success")
            except Exception as error:  # pylint: disable=broad-except
                ctx.logger.warning("FileRegistry update failed: %s", error)
                _record_registry_divergence(ctx, vectors)

        except BaseException as error:
            txn.rollback(type(error).__name__)
            ctx.stats.increment("batch_state_failed_total")
            try:
                _record_ingested_files(
                    ctx,
                    vectors,
                    status="failed",
                    error="fra_transaction_failed",
                )
            except Exception as record_error:  # pylint: disable=broad-except
                ctx.logger.warning("FileRegistry update failed: %s", record_error)
                _record_registry_divergence(ctx, vectors)
            raise


# ---------------------------------------------------------------------------
# FRA supersession helpers
# ---------------------------------------------------------------------------


def _acquire_fra_locks(
    ctx: "IngestContext",
    buildings: list[str],
    timeout_seconds: float,
    lock_lost_event: threading.Event | None = None,
):  # pylint: disable=unused-argument
    """Acquire Redis locks for buildings (assumed de-duplicated)."""
    if getattr(ctx.config, "fra_supersession_single_threaded", False):
        manager = ctx.redis_locks
        building_locks = sorted(buildings)

        class _Ctx:
            def __init__(self):
                self._global_ctx = None
                self._building_ctx = None

            def __enter__(self):
                # Global lock to serialize supersession across workers.
                start = time.monotonic()
                self._global_ctx = manager.lock(
                    "__global__",
                    ttl_ms=int(timeout_seconds * 1000),
                    auto_renew=True,
                    lock_lost_event=lock_lost_event,
                )
                self._global_ctx.__enter__()
                elapsed = time.monotonic() - start
                try:
                    ctx.stats.observe_timing(
                        "fra_supersession_global_lock_wait_seconds", elapsed
                    )
                except Exception:
                    pass
                ctx.logger.info(
                    "FRA supersession global lock acquired in %.3fs", elapsed
                )
                self._building_ctx = manager.lock_many(
                    building_locks,
                    auto_renew=True,
                    lock_lost_event=lock_lost_event,
                )
                return self._building_ctx.__enter__()

            def __exit__(self, exc_type, exc, tb) -> None:
                if self._building_ctx is not None:
                    self._building_ctx.__exit__(exc_type, exc, tb)
                if self._global_ctx is not None:
                    self._global_ctx.__exit__(exc_type, exc, tb)
                    ctx.logger.info("FRA supersession global lock released")

        return _Ctx()

    return ctx.redis_locks.lock_many(
        sorted(buildings),
        auto_renew=True,
        lock_lost_event=lock_lost_event,
    )


def _collect_fra_supersede_requests(
    vectors: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    requests: dict[tuple[str, str], bool] = {}
    for vector in vectors:
        if vector.get("namespace") != FRA_RISK_ITEMS_NAMESPACE:
            continue
        metadata = vector.get("metadata") or {}
        building = metadata.get("canonical_building_name")
        assessment_date = metadata.get("fra_assessment_date")
        if assessment_date == NULL_SENTINEL:
            assessment_date = None
        if building and assessment_date:
            requests[(building, assessment_date)] = True
    return list(requests.keys())


def _filter_supersede_requests_with_registry(
    ctx: "IngestContext",
    requests: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    if not requests:
        return []
    filtered: list[tuple[str, str]] = []
    for building, assessment_date in requests:
        registry_id = f"fra_supersede:{building}:{assessment_date}"
        try:
            existing = ctx.job_registry.get(registry_id)
        except Exception as error:  # pylint: disable=broad-except
            raise ExternalServiceError(
                "FRA idempotency registry lookup unavailable"
            ) from error
        if existing and existing.status == "success":
            ctx.logger.info(
                "Supersession already recorded for %s (assessment: %s); skipping",
                building,
                assessment_date,
            )
            continue
        if existing is not None:
            raise ExternalServiceError(
                "FRA supersession is already active or requires reconciliation"
            )
        filtered.append((building, assessment_date))
    return filtered


class FraVerificationState(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FraVerificationOutcome:
    state: FraVerificationState
    missing_ids: tuple[str, ...] = ()


def _coerce_verification_outcome(
    value: FraVerificationOutcome | list[str],
) -> FraVerificationOutcome:
    """Accept the legacy missing-ID list while callers migrate."""

    if isinstance(value, FraVerificationOutcome):
        return value
    if value:
        return FraVerificationOutcome(
            FraVerificationState.MISSING, tuple(str(item) for item in value)
        )
    return FraVerificationOutcome(FraVerificationState.PRESENT)


def _verify_fra_vectors_present(
    ctx: "IngestContext",
    vectors: list[dict[str, Any]],
    attempts: int = 1,
) -> FraVerificationOutcome:
    fra_ids = [
        vector.get("id")
        for vector in vectors
        if vector.get("namespace") == FRA_RISK_ITEMS_NAMESPACE
    ]
    fra_ids = [vector_id for vector_id in fra_ids if vector_id]
    if not fra_ids:
        return FraVerificationOutcome(FraVerificationState.PRESENT)

    batch_size = INGEST_VERIFY_FETCH_BATCH_SIZE
    missing_ids = set(fra_ids)

    for attempt in range(attempts):
        still_missing = set()
        read_unavailable = False
        for i in range(0, len(fra_ids), batch_size):
            batch = fra_ids[i : i + batch_size]
            try:
                response = ctx.vector_store.fetch(
                    ids=batch, namespace=FRA_RISK_ITEMS_NAMESPACE
                )
            except Exception as error:  # pylint: disable=broad-except
                ctx.logger.error(
                    "FRA verification fetch failed (attempt %d/%d): %s",
                    attempt + 1,
                    attempts,
                    error,
                )
                read_unavailable = True
                continue

            if not response or not getattr(response, "vectors", None):
                still_missing.update(batch)
                continue

            found_ids = set(response.vectors.keys())
            still_missing.update(set(batch) - found_ids)

        if not still_missing:
            if not read_unavailable:
                return FraVerificationOutcome(FraVerificationState.PRESENT)

        missing_ids = still_missing
        if attempt + 1 < attempts:
            backoff_sleep(
                attempt + 1,
                base=INGEST_VERIFY_BACKOFF_BASE,
                cap=INGEST_VERIFY_BACKOFF_CAP,
            )

        if attempt + 1 == attempts and read_unavailable:
            return FraVerificationOutcome(FraVerificationState.UNAVAILABLE)

    return FraVerificationOutcome(
        FraVerificationState.MISSING,
        tuple(sorted(missing_ids)),
    )


def _verify_restored_fra_items(ctx: "IngestContext", item_ids: list[str]) -> bool:
    """Verify rollback metadata for every affected ID using healthy reads."""

    if not item_ids:
        return True
    for start in range(0, len(item_ids), INGEST_VERIFY_FETCH_BATCH_SIZE):
        batch = item_ids[start : start + INGEST_VERIFY_FETCH_BATCH_SIZE]
        try:
            response = ctx.vector_store.fetch(
                ids=batch, namespace=FRA_RISK_ITEMS_NAMESPACE
            )
        except Exception:  # pylint: disable=broad-except
            return False
        vectors = getattr(response, "vectors", None)
        if vectors is None and isinstance(response, dict):
            vectors = response.get("vectors")
        if not isinstance(vectors, dict):
            return False
        for item_id in batch:
            item = vectors.get(item_id)
            if item is None:
                return False
            metadata = (
                item.get("metadata", {})
                if isinstance(item, dict)
                else getattr(item, "metadata", {})
            )
            if metadata.get("is_current") is not True:
                return False
            if metadata.get("superseded_by") not in {None, ""}:
                return False
    return True


# ---------------------------------------------------------------------------
# Batch state / file registry helpers (unchanged)
# ---------------------------------------------------------------------------


def _record_ingested_files(
    ctx: "IngestContext",
    batch: list[dict[str, Any]],
    *,
    status: str,
    error: str | None = None,
) -> None:
    if getattr(ctx.config, "dry_run", False):
        return

    # Rollout fault-injection seam (no-op unless armed in a non-prod env). A
    # fault here exercises the vector-success/registry-write divergence path.
    maybe_fail(FaultPoint.REGISTRY_WRITE)

    batches = [batch]
    completion_tracker = getattr(ctx, "completion_tracker", None)
    failure_statuses: dict[str, str] = {}
    if completion_tracker is not None:
        if status == "success":
            batches = completion_tracker.record_success(batch)
        elif status == "failed":
            failure_statuses = completion_tracker.record_failure(batch)

    if status == "failed" and failure_statuses:
        by_file: dict[str, list[dict[str, Any]]] = {}
        for vector in batch:
            file_id = FileCompletionTracker._file_id(vector)
            if file_id is not None:
                by_file.setdefault(file_id, []).append(vector)
        for file_id, file_batch in by_file.items():
            _write_ingested_file_records(
                ctx,
                file_batch,
                status=failure_statuses.get(file_id, "failed"),
                error=error,
            )
        return

    for completed_batch in batches:
        final_status = status
        final_error = error
        if status == "success" and any(
            vector.get("_file_partial") for vector in completed_batch
        ):
            final_status = "partial"
            final_error = next(
                (
                    str(vector.get("_file_partial_reason"))
                    for vector in completed_batch
                    if vector.get("_file_partial_reason")
                ),
                "embedding_partial",
            )
        override = next(
            (
                vector.get("_file_terminal_status")
                for vector in completed_batch
                if vector.get("_file_terminal_status")
            ),
            None,
        )
        if status == "success" and override:
            final_status = str(override)
            final_error = next(
                (
                    str(vector.get("_file_terminal_reason"))
                    for vector in completed_batch
                    if vector.get("_file_terminal_reason")
                ),
                final_error,
            )
        _write_ingested_file_records(
            ctx,
            completed_batch,
            status=final_status,
            error=final_error,
        )


def _write_ingested_file_records(
    ctx: "IngestContext",
    batch: list[dict[str, Any]],
    *,
    status: str,
    error: str | None,
) -> None:
    """Collapse a completed batch to one registry update per source file."""
    ingested_at_iso = datetime.now(timezone.utc).isoformat() + "Z"
    records: dict[str, dict[str, Any]] = {}
    namespaces: dict[str, set[str]] = {}
    tokens: dict[str, str | None] = {}

    for vector in batch:
        vector_id = vector.get("id")
        if not vector_id:
            continue
        file_id = FileCompletionTracker._file_id(vector)
        if not file_id:
            continue
        metadata = vector.get("metadata") or {}
        ns = get_display_namespace(vector.get("namespace"))
        namespaces.setdefault(file_id, set()).add(ns)
        if file_id not in tokens:
            tokens[file_id] = vector.get("_processing_token")

        if file_id not in records:
            records[file_id] = {
                "file_id": file_id,
                "source_path": metadata.get("source_path", ""),
                "source_key": metadata.get("source") or metadata.get("key") or "",
                "content_hash": metadata.get("content_hash"),
                "ingested_at_iso": ingested_at_iso,
                "status": status,
                "error": error,
            }

    for file_id, payload in records.items():
        ns_tuple = tuple(sorted(namespaces.get(file_id, set())))
        try:
            ctx.file_registry.upsert_with_token(
                FileRecord(
                    file_id=payload["file_id"],
                    source_path=payload["source_path"],
                    source_key=payload["source_key"],
                    content_hash=payload["content_hash"],
                    ingested_at_iso=payload["ingested_at_iso"],
                    namespaces=ns_tuple,
                    status=payload["status"],
                    error=payload["error"],
                ),
                processing_token=tokens.get(file_id),
            )
        except ValueError as error:
            if "token mismatch" in str(error).lower():
                if hasattr(ctx, "stats"):
                    ctx.stats.increment("stale_worker_rejections_total")
            raise
        source_key = payload["source_key"] or file_id
        if hasattr(ctx, "stats") and hasattr(ctx.stats, "record_file_terminal"):
            # For a needs_review file (e.g. fra_no_action_plan carried on the
            # vector override) the error field is the stable review reason.
            reason = (
                payload["error"] if payload["status"] == "needs_review" else None
            )
            ctx.stats.record_file_terminal(source_key, payload["status"], reason)


def _mark_batch_state(
    ctx: "IngestContext",
    batch: list[dict[str, Any]],
    *,
    status: str,
    error: str | None = None,
) -> None:
    ctx.stats.increment(f"batch_state_{status}_total")

    # The state is per file, so collapse the batch to one registry write per
    # file_id rather than one per vector.
    records: dict[str, dict[str, Any]] = {}
    namespaces: dict[str, set[str]] = {}
    for vector in batch:
        vector_id = vector.get("id")
        if not vector_id:
            continue
        file_id = FileCompletionTracker._file_id(vector)
        if not file_id:
            continue
        ns = get_display_namespace(vector.get("namespace"))
        file_namespaces = namespaces.setdefault(file_id, set())
        if ns:
            file_namespaces.add(ns)
        if file_id not in records:
            metadata = vector.get("metadata") or {}
            records[file_id] = {
                "processing_token": vector.get("_processing_token"),
                "source_path": metadata.get("source_path", ""),
                "source_key": metadata.get("source") or metadata.get("key") or "",
                "content_hash": metadata.get("content_hash"),
            }

    for file_id, payload in records.items():
        try:
            ctx.file_registry.mark_state(
                file_id=file_id,
                processing_token=payload["processing_token"],
                status=status,
                error=error,
                source_path=payload["source_path"],
                source_key=payload["source_key"],
                content_hash=payload["content_hash"],
                namespaces=tuple(sorted(namespaces.get(file_id, set()))),
            )
            source_key = payload["source_key"] or file_id
            ctx.stats.record_file_terminal(source_key, status)
        except Exception as err:  # pylint: disable=broad-except
            ctx.logger.warning("FileRegistry state update failed: %s", err)
            if "token mismatch" in str(err).lower():
                ctx.stats.increment("stale_worker_rejections_total")
            ctx.stats.increment("registry_divergence_total")


def _mark_batch_failed(
    ctx: "IngestContext",
    batch: list[dict[str, Any]],
    *,
    reason: str,
) -> None:
    if not batch:
        return
    ctx.stats.increment("batch_state_failed_total")
    try:
        _record_ingested_files(ctx, batch, status="failed", error=reason)
    except Exception as error:  # pylint: disable=broad-except
        ctx.logger.warning("FileRegistry update failed: %s", error)
        ctx.stats.increment("registry_divergence_total")


# ---------------------------------------------------------------------------
# FRA risk-item extraction
# ---------------------------------------------------------------------------


def _create_risk_item_summary(item: EnrichedRiskItem) -> str:
    """Create a searchable summary text for an FRA risk item."""

    def _as_str(value: object, default: str) -> str:
        if value is None:
            return default
        text = str(value).strip()
        return text if text else default

    expected_date = _as_str(item.get("expected_completion_date"), "Not set")
    actual_date = _as_str(item.get("actual_completion_date"), "Not completed")
    issue_number = _as_str(item.get("issue_number"), "Unknown")
    risk_level_text = _as_str(item.get("risk_level_text"), "Unknown")
    risk_level = _as_str(item.get("risk_level"), "?")
    building_name = _as_str(item.get("canonical_building_name"), "Unknown building")
    risk_category = (
        _as_str(item.get("risk_category"), "other").replace("_", " ").title()
    )
    completion_status = _as_str(item.get("completion_status"), "open").upper()
    issue_description = _as_str(
        item.get("issue_description"),
        "No issue description provided.",
    )
    proposed_solution = _as_str(
        item.get("proposed_solution"),
        "No proposed solution provided.",
    )
    person_responsible = _as_str(item.get("person_responsible"), "Not assigned")
    job_reference = _as_str(item.get("job_reference"), "No job reference")

    summary = f"""
Risk Item #{issue_number} - {risk_level_text} Risk (Level {risk_level}/5)
Building: {building_name}
Category: {risk_category}
Status: {completion_status}

Issue Description:
{issue_description}

Proposed Solution:
{proposed_solution}

Responsibility:
Assigned to: {person_responsible}
Job Reference: {job_reference}

Timeline:
Expected Completion: {expected_date}
Actual Completion: {actual_date}
"""

    if item.get("requires_immediate_action"):
        summary += "\nREQUIRES IMMEDIATE ACTION"
    elif item.get("requires_attention"):
        summary += "\nREQUIRES ATTENTION"

    if item.get("flag_overdue"):
        days = _as_str(item.get("days_overdue"), "0")
        summary += f"\nOVERDUE by {days} days"

    if item.get("flag_high_risk_no_job"):
        summary += "\nHIGH RISK - NO JOB REFERENCE"

    figure_refs = item.get("figure_references")
    if isinstance(figure_refs, list) and figure_refs:
        figures = ", ".join(str(fig) for fig in figure_refs)
        summary += f"\n\nEvidence: See Figure(s) {figures}"

    return summary.strip()


def extract_fra_risk_items_integration(
    ctx: "IngestContext",
    *,
    base_path: str,
    key: str,
    text_sample: str,
    building: str,
    content_hash: str | None,
    file_id: str,
    processing_token: str,
    start_time: float,
    vectors_to_upsert: list[dict[str, Any]],
    parse_pool: ProcessPoolExecutor | None = None,
) -> FraVectorExtractResult:
    """
    Extract and embed FRA risk items from a candidate FRA file.
    """
    page_texts = None
    parse_verbose = ctx.config.log_level == "DEBUG"
    triage_computer = FRATriageComputer(verbose=parse_verbose)

    def _run_parse(text_for_parse: str):
        """Parse an action plan, in the process pool when one is available."""
        parse_start = time.perf_counter()
        if parse_pool:
            future = parse_pool.submit(
                parse_action_plan_in_process,
                text_for_parse,
                key,
                building,
                parse_verbose,
            )
            items, conf = future.result()
            where = "process"
        else:
            parser = FRAActionPlanParser(verbose=parse_verbose)
            items, conf = parser.extract_risk_items(
                item_text=text_for_parse,
                item_key=key,
                canonical_building=building,
                page_texts=page_texts,
            )
            where = "thread"
        ctx.logger.debug(
            "FRA parse (%s) %s: %.3fs", where, key, time.perf_counter() - parse_start
        )
        return items, conf

    def _field_score_below(
        scores: dict[str, float],
        field_name: str,
        threshold: float,
    ) -> bool:
        value = scores.get(field_name)
        return value is not None and value < threshold

    # Parse the already-extracted text first. For the common case where the
    # standard text parses cleanly this avoids the pdftotext -layout subprocess
    # (a process spawn plus a second full parse of the PDF).
    risk_items, confidence = _run_parse(text_sample)
    parsing_confidence = getattr(confidence, "overall", 0.0)
    parsing_warnings = list(getattr(confidence, "warnings", []) or [])
    parsing_field_scores = dict(getattr(confidence, "field_scores", {}) or {})

    # Escalate to layout-preserving extraction only when the cheap parse is
    # weak. Layout text parses table-heavy action plans better, but costs the
    # subprocess and a second parse, so it is reserved for low-quality parses.
    if ext(key) == "pdf":
        low_confidence = parsing_confidence < 0.35
        low_fields = _field_score_below(
            parsing_field_scores, "issue_description", 0.5
        ) or _field_score_below(parsing_field_scores, "proposed_solution", 0.5)
        if low_confidence or low_fields:
            layout_text = _extract_fra_layout_text(ctx, base_path=base_path, key=key)
            if layout_text and layout_text != text_sample:
                ctx.logger.warning(
                    "Low-quality FRA text parse for %s (confidence %.2f); "
                    "retrying with layout extraction",
                    key,
                    parsing_confidence,
                )
                layout_items, layout_confidence = _run_parse(layout_text)
                layout_overall = getattr(layout_confidence, "overall", 0.0)
                if layout_overall >= parsing_confidence:
                    risk_items = layout_items
                    parsing_confidence = layout_overall
                    parsing_warnings = list(
                        getattr(layout_confidence, "warnings", []) or []
                    )
                    parsing_field_scores = dict(
                        getattr(layout_confidence, "field_scores", {}) or {}
                    )

    missing_action_plan = (
        not risk_items and "No action plan section found" in parsing_warnings
    )

    if missing_action_plan:
        ctx.stats.increment("fra_action_plan_missing")
        ctx.logger.warning("FRA action plan missing: %s", key)

    if not risk_items:
        ctx.logger.info("No risk items extracted from %s", key)
        return {
            "added": 0,
            "parsing_confidence": parsing_confidence,
            "parsing_warnings": parsing_warnings,
            "parsing_field_scores": parsing_field_scores,
            "missing_action_plan": missing_action_plan,
            "fra_assessment_date": None,
            "fra_assessment_date_int": None,
            "embedding_failures": 0,
        }

    ctx.logger.info(
        "Extracted %d risk items from %s (confidence: %.2f)",
        len(risk_items),
        key,
        parsing_confidence,
    )

    enriched_items: list[EnrichedRiskItem] = [
        triage_computer.compute_flags(item) for item in risk_items
    ]
    assessment_date = None
    assessment_date_int = None
    if enriched_items:
        assessment_date = enriched_items[0].get("fra_assessment_date")
        assessment_date_int = enriched_items[0].get("fra_assessment_date_int")
    enriched_items = deduplicate_risk_items(ctx, enriched_items)
    if not enriched_items:
        ctx.logger.info("All FRA risk items already exist for %s", key)
        return {
            "added": 0,
            "parsing_confidence": parsing_confidence,
            "parsing_warnings": parsing_warnings,
            "parsing_field_scores": parsing_field_scores,
            "missing_action_plan": missing_action_plan,
            "fra_assessment_date": assessment_date,
            "fra_assessment_date_int": assessment_date_int,
            "embedding_failures": 0,
        }

    if getattr(ctx.config, "dry_run", False):
        ctx.logger.info("Dry-run: skipping FRA risk item embeddings for %s", key)
        return {
            "added": 0,
            "parsing_confidence": parsing_confidence,
            "parsing_warnings": parsing_warnings,
            "parsing_field_scores": parsing_field_scores,
            "missing_action_plan": missing_action_plan,
            "fra_assessment_date": assessment_date,
            "fra_assessment_date_int": assessment_date_int,
            "embedding_failures": 0,
        }

    summaries: list[str] = []
    for item in enriched_items:
        summaries.append(_create_risk_item_summary(item))

    max_seconds = getattr(ctx.config, "max_file_seconds", 0)
    if max_seconds > 0 and (time.perf_counter() - start_time) > max_seconds:
        ctx.file_registry.mark_state(
            file_id=file_id,
            processing_token=processing_token,
            status="failed",
            error="file_timeout",
            source_path=base_path,
            source_key=key,
            content_hash=content_hash,
        )
        raise IngestError(f"File processing timed out: {key}")

    embed_start = time.perf_counter()
    result = embed_texts_batch(ctx, summaries)
    if result.fatal_error:
        raise IngestError("FRA embedding configuration is invalid")
    if result.errors_by_index and not result.embeddings_by_index:
        raise ExternalServiceError("FRA embedding service is unavailable")
    embeddings_by_index: dict[int, list[float]] = result.embeddings_by_index
    embed_elapsed = time.perf_counter() - embed_start
    ctx.logger.debug(
        "FRA embedding batch %s: %.3fs (%d items)",
        key,
        embed_elapsed,
        len(summaries),
    )
    if max_seconds > 0 and (time.perf_counter() - start_time) > max_seconds:
        ctx.file_registry.mark_state(
            file_id=file_id,
            processing_token=processing_token,
            status="failed",
            error="file_timeout",
            source_path=base_path,
            source_key=key,
            content_hash=content_hash,
        )
        raise IngestError(f"File processing timed out: {key}")
    if result.errors_by_index:
        ctx.logger.warning(
            "Embedding batch had %d failures for %s; skipping failed items",
            len(result.errors_by_index),
            key,
        )
        for idx in result.errors_by_index:
            ctx.stats.increment("fra_embeddings_failed")

    added = 0
    for idx, item in enumerate(enriched_items):
        if idx in result.errors_by_index:
            continue
        summary_text = summaries[idx]
        embedding = embeddings_by_index.get(idx)
        if not embedding:
            ctx.logger.warning(
                "Skipping risk item %s due to missing embedding",
                item.get("risk_item_id"),
            )
            continue

        metadata = {
            **sanitise_risk_item_for_metadata(item),
            "source_path": base_path,
            "key": key,
            "source": key,
            "content_hash": content_hash,
            "parsing_confidence": parsing_confidence,
            "parsing_warnings": parsing_warnings,
            "parsing_field_scores": parsing_field_scores,
            "document_type": DocumentTypes.FRA_RISK_ITEM,
        }
        canonical_building = item.get("canonical_building_name")
        if canonical_building:
            metadata["partition_key"] = _fra_partition_key(canonical_building)
        metadata["text"] = summary_text

        valid, reason = validate_with_truncation(
            ctx,
            metadata,
            logger=ctx.logger,
        )
        if not valid:
            ctx.logger.warning(
                "Invalid FRA risk-item metadata for %s: %s",
                item.get("risk_item_id"),
                reason,
            )
            continue

        vectors_to_upsert.append(
            {
                "id": item["risk_item_id"],
                "values": embedding,
                "metadata": metadata,
                "namespace": FRA_RISK_ITEMS_NAMESPACE,
                "_file_id": file_id,
                "_processing_token": processing_token,
            }
        )
        added += 1

    return {
        "added": added,
        "parsing_confidence": parsing_confidence,
        "parsing_warnings": parsing_warnings,
        "parsing_field_scores": parsing_field_scores,
        "missing_action_plan": missing_action_plan,
        "fra_assessment_date": assessment_date,
        "fra_assessment_date_int": assessment_date_int,
        "embedding_failures": len(result.errors_by_index),
    }
