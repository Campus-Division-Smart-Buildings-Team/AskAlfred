"""INGEST-08: distinct empty/review terminal reasons, separate from failure.

A file that produces no usable vectors, or an FRA with no action-plan section,
must finish ``needs_review`` with a *specific* reason an operator can act on —
``empty_document``, ``unsupported_layout``, or ``fra_no_action_plan`` — never a
generic ``no_usable_vectors`` and never a technical ``failed``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.ingest_outcomes import (
    NEEDS_REVIEW_REASONS,
    REVIEW_EMPTY_DOCUMENT,
    REVIEW_FRA_NO_ACTION_PLAN,
    REVIEW_UNSUPPORTED_LAYOUT,
    IngestTerminalStatus,
)
from core.telemetry import METRIC_INGEST_REVIEW, get_telemetry
from ingest.batch_ingest import IngestReport, _log_ingest_summary
from ingest.document_processor import (
    DocumentProcessor,
    FileIngestOrchestrator,
    _has_extractable_text,
    _no_vector_review_reason,
)
from ingest.transaction import (
    FileCompletionTracker,
    ThreadSafeStats,
    _record_ingested_files,
)


@pytest.fixture(autouse=True)
def _reset_telemetry():
    get_telemetry().reset()
    yield
    get_telemetry().reset()


# --- classification --------------------------------------------------------


def test_no_text_anywhere_is_empty_document():
    assert _no_vector_review_reason("", []) == REVIEW_EMPTY_DOCUMENT
    assert _no_vector_review_reason(None, [("k", "B", "", {})]) == REVIEW_EMPTY_DOCUMENT
    assert (
        _no_vector_review_reason("   \n ", [("k", "B", "  ", {})])
        == REVIEW_EMPTY_DOCUMENT
    )


def test_extracted_text_without_vectors_is_unsupported_layout():
    assert (
        _no_vector_review_reason("has content", []) == REVIEW_UNSUPPORTED_LAYOUT
    )
    assert (
        _no_vector_review_reason("", [("k", "B", "row text", {})])
        == REVIEW_UNSUPPORTED_LAYOUT
    )


def test_has_extractable_text_helper():
    assert _has_extractable_text("x", []) is True
    assert _has_extractable_text("", [("k", "B", "x", {})]) is True
    assert _has_extractable_text("", [("k", "B", "", {})]) is False


def test_all_review_reasons_are_registered_constants():
    assert {
        REVIEW_EMPTY_DOCUMENT,
        REVIEW_UNSUPPORTED_LAYOUT,
        REVIEW_FRA_NO_ACTION_PLAN,
    } == NEEDS_REVIEW_REASONS


# --- processor behaviour ---------------------------------------------------


class _FakeEncoder:
    @staticmethod
    def encode(text):
        return list(text)

    @staticmethod
    def decode(tokens):
        return "".join(tokens)


class _Resolution:
    canonical = "Test Building"
    confidence = 1.0
    source = "filename"


class _FakeResolver:
    def resolve(self, _key, _text):
        return _Resolution()


def _make_orchestrator(data: bytes):
    registry = Mock()
    stats = ThreadSafeStats()
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            embed_model="text-embedding-3-small",
            chunk_tokens=100,
            chunk_overlap=10,
            embed_batch=10,
            dry_run=False,
            max_file_mb=0,
            max_file_seconds=0,
        ),
        logger=logging.getLogger("ingest-08-test"),
        stats=stats,
        encoder=_FakeEncoder(),
        file_registry=registry,
        completion_tracker=FileCompletionTracker(),
    )
    processor = DocumentProcessor(
        ctx=ctx,
        base_path="/data",
        alias_to_canonical={},
        fra_vector_extractor=lambda *a, **k: {},
        building_resolver=_FakeResolver(),
    )
    # Skip the disk/registry lease; drive extraction + terminal classification.
    processor.load_bytes_and_ids = lambda key: (data, "hash", "file", "token")
    processor.maybe_extract_fra_vectors = lambda **k: False
    processor.build_vectors_from_docs = lambda **k: []
    return FileIngestOrchestrator(processor), registry, stats


def test_empty_file_finishes_needs_review_empty_document():
    orchestrator, registry, stats = _make_orchestrator(b"")

    result = orchestrator.process({"Key": "empty.txt", "Size": 0})

    assert result.status == "needs_review"
    assert result.review_reason == REVIEW_EMPTY_DOCUMENT
    mark = registry.mark_state.call_args.kwargs
    assert mark["status"] == "needs_review"
    assert mark["error"] == REVIEW_EMPTY_DOCUMENT
    assert stats.get_stats()["review_reasons"] == {REVIEW_EMPTY_DOCUMENT: 1}
    assert (
        get_telemetry().get(METRIC_INGEST_REVIEW, reason=REVIEW_EMPTY_DOCUMENT) == 1
    )


def test_unparseable_content_finishes_needs_review_unsupported_layout():
    orchestrator, registry, stats = _make_orchestrator(b"real document content")

    result = orchestrator.process({"Key": "brochure.txt", "Size": 21})

    assert result.status == "needs_review"
    assert result.review_reason == REVIEW_UNSUPPORTED_LAYOUT
    mark = registry.mark_state.call_args.kwargs
    assert mark["status"] == "needs_review"
    assert mark["error"] == REVIEW_UNSUPPORTED_LAYOUT
    assert stats.get_stats()["review_reasons"] == {REVIEW_UNSUPPORTED_LAYOUT: 1}


def _vector(vector_id: str) -> dict:
    return {
        "id": vector_id,
        "values": [0.1],
        "metadata": {"source": "fra.pdf", "source_path": "/data"},
        "namespace": "fire_risk_assessments",
        "_processing_token": "token",
    }


def test_fra_no_action_plan_override_finishes_needs_review():
    registry = Mock()
    ctx = SimpleNamespace(
        config=SimpleNamespace(dry_run=False),
        file_registry=registry,
        stats=ThreadSafeStats(),
    )
    vector = {
        **_vector("file:doc:0"),
        "_file_terminal_status": "needs_review",
        "_file_terminal_reason": REVIEW_FRA_NO_ACTION_PLAN,
    }

    _record_ingested_files(ctx, [vector], status="success")

    record = registry.upsert_with_token.call_args.args[0]
    assert record.status == "needs_review"
    assert record.error == REVIEW_FRA_NO_ACTION_PLAN
    assert ctx.stats.get_stats()["review_reasons"] == {REVIEW_FRA_NO_ACTION_PLAN: 1}
    assert (
        get_telemetry().get(METRIC_INGEST_REVIEW, reason=REVIEW_FRA_NO_ACTION_PLAN)
        == 1
    )


# --- stats tallying --------------------------------------------------------


def test_review_reason_counted_once_across_repeated_writes():
    stats = ThreadSafeStats()

    stats.record_file_terminal("a.pdf", "needs_review", REVIEW_EMPTY_DOCUMENT)
    stats.record_file_terminal("a.pdf", "needs_review", REVIEW_EMPTY_DOCUMENT)

    assert stats.get_stats()["review_reasons"] == {REVIEW_EMPTY_DOCUMENT: 1}
    assert (
        get_telemetry().get(METRIC_INGEST_REVIEW, reason=REVIEW_EMPTY_DOCUMENT) == 1
    )


def test_non_review_status_does_not_record_a_reason():
    stats = ThreadSafeStats()

    stats.record_file_terminal("a.pdf", "partial", "embedding_partial")

    assert stats.get_stats()["review_reasons"] == {}


# --- CLI summary -----------------------------------------------------------


def test_summary_reports_needs_review_count_and_reason_breakdown(caplog):
    report = IngestReport(
        files_found=3,
        files_processed=3,
        files_skipped=0,
        files_failed=0,
        total_vectors=0,
        duration_seconds=1.0,
        status=IngestTerminalStatus.NEEDS_REVIEW,
        files_needs_review=3,
        review_reasons={
            REVIEW_EMPTY_DOCUMENT: 1,
            REVIEW_UNSUPPORTED_LAYOUT: 1,
            REVIEW_FRA_NO_ACTION_PLAN: 1,
        },
    )
    ctx = SimpleNamespace(logger=logging.getLogger("ingest-08-summary"))

    with caplog.at_level(logging.INFO):
        _log_ingest_summary(ctx, report)

    text = caplog.text
    assert "Files needs review:   3" in text
    assert f"{REVIEW_EMPTY_DOCUMENT}: 1" in text
    assert f"{REVIEW_UNSUPPORTED_LAYOUT}: 1" in text
    assert f"{REVIEW_FRA_NO_ACTION_PLAN}: 1" in text
