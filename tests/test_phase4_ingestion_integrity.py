"""Phase 4 ingestion/FRA integrity acceptance coverage."""

from __future__ import annotations

import logging
import threading
from contextlib import nullcontext
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.alfred_exceptions import CriticalInconsistentError, ExternalServiceError
from core.ingest_outcomes import (
    IngestExitCode,
    IngestTerminalStatus,
    exit_code_for_status,
)
from core.telemetry import METRIC_INGEST_OUTCOME, get_telemetry
from ingest.batch_ingest import (
    WorkerTeardownReport,
    _derive_run_status,
    _teardown_worker,
)
from ingest.fra_reconciliation import reconcile_fra_transactions
from ingest.registry_reconciliation import (
    reconcile_registry_divergence,
    spool_registry_divergence,
)
from ingest.transaction import (
    FileCompletionTracker,
    FraVerificationOutcome,
    FraVerificationState,
    ThreadSafeStats,
    _record_ingested_files,
    upsert_vectors_atomic,
)
from interfaces import (
    FraJournalState,
    InMemoryFraTransactionJournal,
    new_fra_journal_record,
)


@pytest.fixture(autouse=True)
def _reset_telemetry():
    get_telemetry().reset()
    yield
    get_telemetry().reset()


def _ctx() -> SimpleNamespace:
    job_registry = Mock()
    job_registry.get.return_value = None
    journal = InMemoryFraTransactionJournal()
    return SimpleNamespace(
        config=SimpleNamespace(
            dry_run=False,
            fra_supersession_single_threaded=False,
            upsert_join_timeout_seconds=0.01,
            upsert_join_poll_seconds=0.001,
        ),
        stats=ThreadSafeStats(),
        logger=logging.getLogger("phase4-test"),
        upsert_stop_event=None,
        fra_journal=journal,
        job_registry=job_registry,
        redis_locks=SimpleNamespace(lock_many=lambda *a, **k: nullcontext()),
        event_sink=SimpleNamespace(emit_event=lambda event: None),
    )


def _fra_vector() -> dict:
    return {
        "id": "file:risk:1",
        "values": [0.1],
        "namespace": "fra_risk_items",
        "metadata": {
            "canonical_building_name": "Test Building",
            "fra_assessment_date": "2026-01-01",
            "source": "test.pdf",
        },
        "_processing_token": "token",
    }


def test_exit_code_contract_is_stable():
    assert exit_code_for_status(IngestTerminalStatus.SUCCESS) == IngestExitCode.SUCCESS
    assert exit_code_for_status("empty_input") == IngestExitCode.EMPTY_OR_VALIDATION
    assert exit_code_for_status("partial") == IngestExitCode.PARTIAL
    assert exit_code_for_status("unavailable") == IngestExitCode.UNAVAILABLE
    assert exit_code_for_status("failed") == IngestExitCode.FAILED
    assert (
        exit_code_for_status("critical_inconsistent")
        == IngestExitCode.CRITICAL_INCONSISTENT
    )


def test_run_status_cannot_hide_partial_files_or_registry_divergence():
    ctx = _ctx()
    status = _derive_run_status(
        ctx=ctx,
        files_found=2,
        file_outcomes={"a": "success", "b": "partial"},
        upsert_errors=[],
        teardown=WorkerTeardownReport(),
    )
    assert status is IngestTerminalStatus.PARTIAL

    ctx.stats.increment("registry_divergence_total")
    status = _derive_run_status(
        ctx=ctx,
        files_found=1,
        file_outcomes={"a": "success"},
        upsert_errors=[],
        teardown=WorkerTeardownReport(),
    )
    assert status is IngestTerminalStatus.PARTIAL


def test_file_terminal_state_is_counted_once_without_identifier_label():
    stats = ThreadSafeStats()
    stats.record_file_terminal("secret/path/a.pdf", "partial")
    stats.record_file_terminal("secret/path/a.pdf", "partial")

    assert stats.get_stats()["file_terminal_states"] == {
        "secret/path/a.pdf": "partial"
    }
    assert (
        get_telemetry().get(
            METRIC_INGEST_OUTCOME, scope="file", status="partial"
        )
        == 1
    )


def test_unclassified_execute_exception_always_rolls_back(monkeypatch):
    ctx = _ctx()
    restored: list[str] = []

    def supersede(*, before_update, **kwargs):
        before_update(["old-risk"])
        return ["old-risk"]

    monkeypatch.setattr("ingest.transaction.mark_superseded_risk_items", supersede)
    monkeypatch.setattr(
        "ingest.transaction.upsert_vectors",
        lambda *a, **k: (_ for _ in ()).throw(KeyError("unclassified")),
    )
    monkeypatch.setattr(
        "ingest.transaction.restore_superseded_items",
        lambda ctx, ids: restored.extend(ids) or len(ids),
    )
    monkeypatch.setattr("ingest.transaction._verify_restored_fra_items", lambda *a: True)
    monkeypatch.setattr("ingest.transaction._record_ingested_files", lambda *a, **k: None)

    with pytest.raises(KeyError):
        upsert_vectors_atomic(ctx, [_fra_vector()])

    assert restored == ["old-risk"]
    records = list(ctx.fra_journal._records.values())  # test implementation snapshot
    assert records[0].state is FraJournalState.ROLLED_BACK


def test_verification_read_outage_does_not_trigger_rollback(monkeypatch):
    ctx = _ctx()
    rollback = Mock(return_value=1)

    def supersede(*, before_update, **kwargs):
        before_update(["old-risk"])
        return ["old-risk"]

    monkeypatch.setattr("ingest.transaction.mark_superseded_risk_items", supersede)
    monkeypatch.setattr("ingest.transaction.upsert_vectors", lambda *a, **k: None)
    monkeypatch.setattr(
        "ingest.transaction._verify_fra_vectors_present",
        lambda *a, **k: FraVerificationOutcome(FraVerificationState.UNAVAILABLE),
    )
    monkeypatch.setattr("ingest.transaction.restore_superseded_items", rollback)
    monkeypatch.setattr("ingest.transaction._record_ingested_files", lambda *a, **k: None)

    upsert_vectors_atomic(ctx, [_fra_vector()])

    rollback.assert_not_called()
    record = ctx.fra_journal.list_open()[0]
    assert record.state is FraJournalState.VERIFICATION_UNAVAILABLE
    assert ctx.fra_journal.blocking_transaction("Test Building") == record.tx_id


def test_incomplete_rollback_blocks_building_and_is_critical(monkeypatch):
    ctx = _ctx()

    def supersede(*, before_update, **kwargs):
        before_update(["old-a", "old-b"])
        return ["old-a", "old-b"]

    monkeypatch.setattr("ingest.transaction.mark_superseded_risk_items", supersede)
    monkeypatch.setattr(
        "ingest.transaction.upsert_vectors",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr("ingest.transaction.restore_superseded_items", lambda *a: 1)
    monkeypatch.setattr("ingest.transaction._verify_restored_fra_items", lambda *a: False)

    with pytest.raises(CriticalInconsistentError):
        upsert_vectors_atomic(ctx, [_fra_vector()])

    record = ctx.fra_journal.list_open()[0]
    assert record.state is FraJournalState.CRITICAL_INCONSISTENT
    assert ctx.fra_journal.blocking_transaction("Test Building") == record.tx_id
    assert ctx.stats.get_stats()["critical_inconsistent_total"] == 1


def test_crash_journal_is_recoverable_and_reconciliation_is_idempotent(monkeypatch):
    ctx = _ctx()
    record = new_fra_journal_record(
        tx_id="tx-crash",
        buildings=["Test Building"],
        requests=[("Test Building", "2026-01-01")],
        vector_ids=["new-risk"],
    )
    ctx.fra_journal.begin(record)
    ctx.fra_journal.append_superseded("tx-crash", ["old-risk"])
    ctx.fra_journal.transition("tx-crash", FraJournalState.SUPERSEDED)
    ctx.fra_journal.block_buildings("tx-crash", ["Test Building"])
    monkeypatch.setattr("ingest.fra_reconciliation.restore_superseded_items", lambda *a: 1)
    monkeypatch.setattr(
        "ingest.fra_reconciliation._verify_restored_fra_items", lambda *a: True
    )

    first = reconcile_fra_transactions(ctx)
    second = reconcile_fra_transactions(ctx)

    assert first.status is IngestTerminalStatus.SUCCESS
    assert first.reconciled == 1
    assert second.reconciled == 0
    assert ctx.fra_journal.get("tx-crash").state is FraJournalState.ROLLED_BACK
    assert ctx.fra_journal.blocking_transaction("Test Building") is None


def test_queue_timeout_and_lingering_worker_are_explicit(monkeypatch):
    ctx = _ctx()
    queue: Queue = Queue()
    queue.put(None)
    alive = Mock()
    alive.name = "stuck-worker"
    alive.is_alive.return_value = True
    alive.join.return_value = None
    monkeypatch.setattr("ingest.batch_ingest.time.sleep", lambda _: None)

    report = _teardown_worker(
        ctx,
        upsert_stop_event=threading.Event(),
        upsert_queue=queue,
        upsert_threads=[alive],
    )

    assert report.lingering_workers == ("stuck-worker",)


def test_fra_idempotency_registry_read_failure_fails_closed():
    from ingest.transaction import _filter_supersede_requests_with_registry

    ctx = _ctx()
    ctx.job_registry.get.side_effect = OSError("redis unavailable")
    with pytest.raises(ExternalServiceError):
        _filter_supersede_requests_with_registry(
            ctx, [("Test Building", "2026-01-01")]
        )


def test_successful_chunks_plus_singleton_failure_finish_partial():
    tracker = FileCompletionTracker()
    registry = Mock()
    ctx = SimpleNamespace(
        config=SimpleNamespace(dry_run=False),
        completion_tracker=tracker,
        file_registry=registry,
        stats=ThreadSafeStats(),
    )
    first = {
        "id": "file:a:0",
        "metadata": {"source": "a.pdf"},
        "namespace": "docs",
        "_processing_token": "token",
    }
    second = {**first, "id": "file:a:1"}
    tracker.register([first, second])

    _record_ingested_files(ctx, [first], status="success")
    _record_ingested_files(ctx, [second], status="failed", error="singleton_failed")

    record = registry.upsert_with_token.call_args.args[0]
    assert record.status == "partial"
    assert ctx.stats.get_stats()["file_terminal_states"]["a.pdf"] == "partial"


def test_registry_divergence_spool_replays_idempotent_partial(tmp_path):
    path = tmp_path / "registry-reconcile.jsonl"
    registry = Mock()
    ctx = SimpleNamespace(
        config=SimpleNamespace(registry_reconciliation_file=str(path)),
        file_registry=registry,
        export_events_lock=threading.Lock(),
    )
    vectors = [
        {
            "id": "file:a:0",
            "namespace": "docs",
            "_processing_token": "token",
            "metadata": {
                "source_path": "/data",
                "source": "a.pdf",
                "content_hash": "abc",
            },
        }
    ]

    assert spool_registry_divergence(ctx, vectors) == 1
    report = reconcile_registry_divergence(ctx)

    assert report.status is IngestTerminalStatus.SUCCESS
    assert report.reconciled == 1
    assert path.read_text(encoding="utf-8") == ""
    assert registry.mark_state.call_args.kwargs["status"] == "partial"
