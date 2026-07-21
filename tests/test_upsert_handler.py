#!/usr/bin/env python3
"""Tests for the upsert worker's not-before re-queue behaviour."""

import threading
import time
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import ingest.upsert_handler as upsert_handler
from core.alfred_exceptions import RetriableError
from ingest.helpers import UpsertQueueItem


class RecordingQueue(Queue):
    """Queue that records every item put on it (including re-queues)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recorded = []

    def put(self, item, block=True, timeout=None):
        self.recorded.append(item)
        super().put(item, block=block, timeout=timeout)


@pytest.fixture
def ctx():
    return SimpleNamespace(stats=Mock(), logger=Mock(), event_sink=Mock())


@pytest.fixture
def fast_backoff(monkeypatch):
    """Deterministic 0.4s backoff for every retry attempt."""
    monkeypatch.setattr(upsert_handler, "INGEST_BACKOFF_BASE", 0.4)
    monkeypatch.setattr(upsert_handler, "INGEST_BACKOFF_CAP", 0.4)
    monkeypatch.setattr(upsert_handler, "INGEST_BACKOFF_JITTER_MIN", 0.0)
    monkeypatch.setattr(upsert_handler, "INGEST_BACKOFF_JITTER_SPAN", 0.0)
    return 0.4


def _start_worker(ctx, queue, stop_event, errors):
    thread = threading.Thread(
        target=upsert_handler._upsert_worker,
        args=(ctx, queue, stop_event, errors),
        daemon=True,
    )
    thread.start()
    return thread


def _wait_for_drain(queue, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queue.unfinished_tasks == 0:
            return True
        time.sleep(0.01)
    return False


def _vector(vector_id):
    return {"id": vector_id, "values": [0.0], "metadata": {}}


def test_retry_requeues_with_deadline_and_keeps_worker_available(
    ctx, fast_backoff, monkeypatch
):
    queue = RecordingQueue()
    stop_event = threading.Event()
    completions = {}
    first_failure = []

    def fake_upsert(_ctx, batch):
        marker = batch[0]["id"]
        if marker == "a:0" and not first_failure:
            first_failure.append(time.monotonic())
            raise RetriableError("transient failure")
        completions[marker] = time.monotonic()

    monkeypatch.setattr(upsert_handler, "upsert_vectors_atomic", fake_upsert)
    monkeypatch.setattr(upsert_handler, "_mark_batch_failed", Mock())

    batch_a = [_vector("a:0")]
    batch_b = [_vector("b:0")]
    queue.put(UpsertQueueItem(batch_a))
    queue.put(UpsertQueueItem(batch_b))

    thread = _start_worker(ctx, queue, stop_event, [])
    assert _wait_for_drain(queue)
    queue.put(None)
    thread.join(timeout=5)
    assert not thread.is_alive()

    failed_at = first_failure[0]
    # The single worker processed B during A's backoff window instead of
    # sleeping through it (the old behaviour blocked B for the full backoff).
    assert completions["b:0"] - failed_at < fast_backoff / 2
    # A's retry still honoured its not-before deadline.
    assert completions["a:0"] - failed_at >= fast_backoff - 0.1

    requeued = [
        item
        for item in queue.recorded
        if isinstance(item, UpsertQueueItem) and item.retry_index == 1
    ]
    assert len(requeued) == 1
    assert requeued[0].batch == batch_a
    assert requeued[0].not_before > failed_at


def test_delayed_item_executes_after_deadline(ctx, monkeypatch):
    queue = Queue()
    stop_event = threading.Event()
    executed = []
    monkeypatch.setattr(
        upsert_handler,
        "upsert_vectors_atomic",
        lambda _ctx, _batch: executed.append(time.monotonic()),
    )

    deadline = time.monotonic() + 0.3
    queue.put(UpsertQueueItem([_vector("v:0")], not_before=deadline))

    thread = _start_worker(ctx, queue, stop_event, [])
    assert _wait_for_drain(queue)
    queue.put(None)
    thread.join(timeout=5)

    assert executed and executed[0] >= deadline - 0.05


def test_stop_event_interrupts_backoff_wait(ctx, monkeypatch):
    queue = Queue()
    stop_event = threading.Event()
    write_stub = Mock()
    mark_failed = Mock()
    monkeypatch.setattr(upsert_handler, "upsert_vectors_atomic", write_stub)
    monkeypatch.setattr(upsert_handler, "_mark_batch_failed", mark_failed)

    batch = [_vector("v:0")]
    queue.put(UpsertQueueItem(batch, retry_index=1, not_before=time.monotonic() + 30.0))

    thread = _start_worker(ctx, queue, stop_event, [])
    time.sleep(0.2)  # let the worker dequeue and start waiting
    stop_event.set()

    # Far sooner than the 30s deadline — the old time.sleep was uninterruptible
    assert _wait_for_drain(queue, timeout=2.0)
    queue.put(None)
    thread.join(timeout=5)
    assert not thread.is_alive()

    mark_failed.assert_called_once_with(ctx, batch, reason="shutdown_during_retry")
    write_stub.assert_not_called()


def test_exhausted_retries_split_batch_into_halves(ctx, monkeypatch):
    queue = RecordingQueue()
    stop_event = threading.Event()

    def fake_upsert(_ctx, batch):
        if len(batch) == 24:
            raise RetriableError("persistent failure")

    monkeypatch.setattr(upsert_handler, "upsert_vectors_atomic", fake_upsert)
    monkeypatch.setattr(upsert_handler, "_mark_batch_failed", Mock())

    batch = [_vector(f"v:{i}") for i in range(24)]
    queue.put(UpsertQueueItem(batch, retry_index=upsert_handler.INGEST_RETRY_ATTEMPTS))

    thread = _start_worker(ctx, queue, stop_event, [])
    assert _wait_for_drain(queue)
    queue.put(None)
    thread.join(timeout=5)

    halves = [
        item
        for item in queue.recorded
        if isinstance(item, UpsertQueueItem) and item.split_depth == 1
    ]
    assert [len(item.batch) for item in halves] == [12, 12]
    assert all(item.retry_index == 0 for item in halves)
    assert all(item.not_before == 0.0 for item in halves)


# ---------------------------------------------------------------------------
# VECTOR-06: aggregate retry budget across retries and recursive splits
# ---------------------------------------------------------------------------


def test_next_action_fails_when_aggregate_budget_exhausted():
    """Once the lineage has spent the aggregate retry budget, a retryable error
    yields an explicit terminal failure rather than another retry or split."""
    action = upsert_handler.UpsertPolicy.next_action(
        RetriableError("transient"),
        retry_index=0,
        split_depth=0,
        batch_size=100,
        retries_consumed=upsert_handler.INGEST_UPSERT_MAX_TOTAL_RETRIES,
    )
    assert isinstance(action, upsert_handler._FailAction)
    assert action.reason == upsert_handler._RETRY_BUDGET_EXHAUSTED_REASON


def test_next_action_retries_and_splits_within_budget():
    """With budget remaining, behaviour is unchanged: retry while per-batch
    retries remain, then split."""
    retry = upsert_handler.UpsertPolicy.next_action(
        RetriableError("transient"),
        retry_index=0,
        split_depth=0,
        batch_size=100,
        retries_consumed=0,
    )
    assert isinstance(retry, upsert_handler._RetryAction)

    split = upsert_handler.UpsertPolicy.next_action(
        RetriableError("transient"),
        retry_index=upsert_handler.INGEST_RETRY_ATTEMPTS,
        split_depth=0,
        batch_size=100,
        retries_consumed=upsert_handler.INGEST_RETRY_ATTEMPTS,
    )
    assert isinstance(split, upsert_handler._SplitAction)


def test_split_children_inherit_consumed_retries(ctx, monkeypatch):
    """A split carries the lineage's consumed-retry count to both children so
    splitting cannot refresh the aggregate retry budget."""
    queue = RecordingQueue()
    stop_event = threading.Event()

    def fake_upsert(_ctx, batch):
        if len(batch) == 24:
            raise RetriableError("persistent failure")

    monkeypatch.setattr(upsert_handler, "upsert_vectors_atomic", fake_upsert)
    monkeypatch.setattr(upsert_handler, "_mark_batch_failed", Mock())

    batch = [_vector(f"v:{i}") for i in range(24)]
    queue.put(
        UpsertQueueItem(
            batch,
            retry_index=upsert_handler.INGEST_RETRY_ATTEMPTS,
            retries_consumed=2,
        )
    )

    thread = _start_worker(ctx, queue, stop_event, [])
    assert _wait_for_drain(queue)
    queue.put(None)
    thread.join(timeout=5)

    halves = [
        item
        for item in queue.recorded
        if isinstance(item, UpsertQueueItem) and item.split_depth == 1
    ]
    assert [len(item.batch) for item in halves] == [12, 12]
    assert all(item.retries_consumed == 2 for item in halves)


def test_aggregate_retry_budget_exhaustion_is_terminal(ctx, monkeypatch):
    """A persistently retryable batch that keeps splitting terminates with an
    explicit budget-exhausted outcome and telemetry instead of spinning."""
    from core.telemetry import get_telemetry

    get_telemetry().reset()
    for name in (
        "INGEST_BACKOFF_BASE",
        "INGEST_BACKOFF_CAP",
        "INGEST_BACKOFF_JITTER_MIN",
        "INGEST_BACKOFF_JITTER_SPAN",
    ):
        monkeypatch.setattr(upsert_handler, name, 0.0)
    # Let batches split to singletons and cap the aggregate retries low so the
    # scenario resolves quickly and deterministically.
    monkeypatch.setattr(upsert_handler, "INGEST_UPSERT_SPLIT_MIN_BATCH_SIZE", 1)
    monkeypatch.setattr(upsert_handler, "INGEST_UPSERT_MAX_TOTAL_RETRIES", 4)

    queue = RecordingQueue()
    stop_event = threading.Event()
    attempts = {"count": 0}

    def fake_upsert(_ctx, _batch):
        attempts["count"] += 1
        raise RetriableError("always fails")

    monkeypatch.setattr(upsert_handler, "upsert_vectors_atomic", fake_upsert)
    failed_reasons: list[str] = []
    monkeypatch.setattr(
        upsert_handler,
        "_mark_batch_failed",
        lambda _ctx, _batch, *, reason: failed_reasons.append(reason),
    )

    batch = [_vector("v:0"), _vector("v:1")]
    queue.put(UpsertQueueItem(batch))

    thread = _start_worker(ctx, queue, stop_event, [])
    assert _wait_for_drain(queue)
    queue.put(None)
    thread.join(timeout=5)
    assert not thread.is_alive()

    # The lineage terminated on the budget, not on min-batch/depth guards.
    assert upsert_handler._RETRY_BUDGET_EXHAUSTED_REASON in failed_reasons
    ctx.stats.increment.assert_any_call("upsert_retry_budget_exhausted_total")
    assert (
        get_telemetry().get(
            "ingest_integrity_total",
            event="upsert",
            state="retry_budget_exhausted",
        )
        >= 1
    )


def test_retry_records_idempotent_rewrite_metric(ctx, fast_backoff, monkeypatch):
    """A retry re-sends the same vector IDs; that idempotent rewrite volume is
    published for observability (VECTOR-06)."""
    queue = Queue()
    stop_event = threading.Event()
    first_failure: list[int] = []

    def fake_upsert(_ctx, _batch):
        if not first_failure:
            first_failure.append(1)
            raise RetriableError("transient failure")

    monkeypatch.setattr(upsert_handler, "upsert_vectors_atomic", fake_upsert)
    monkeypatch.setattr(upsert_handler, "_mark_batch_failed", Mock())

    batch = [_vector("a:0"), _vector("a:1")]
    queue.put(UpsertQueueItem(batch))

    thread = _start_worker(ctx, queue, stop_event, [])
    assert _wait_for_drain(queue)
    queue.put(None)
    thread.join(timeout=5)

    ctx.stats.increment.assert_any_call("upsert_idempotent_rewrites_total", 2)
