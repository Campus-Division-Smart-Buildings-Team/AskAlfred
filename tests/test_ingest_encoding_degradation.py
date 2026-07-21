"""INGEST-06: report lossy text-decoding as a degraded ingestion outcome.

These tests cover the whole path a lossy encoding fallback travels:

1. ``extract_text_with_provenance`` records a stable fallback reason.
2. The reason survives the extract/chunk process-pool boundary and CSV
   fallbacks.
3. ``DocumentProcessor``/``Vectoriser`` turn the reason into a ``degraded``
   file-terminal override that ``partial``/``needs_review`` still outrank.
4. The registry, run aggregation, and exit-code contract treat ``degraded`` as
   a completed-but-reduced-fidelity terminal state, never plain ``success``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.ingest_outcomes import (
    IngestExitCode,
    IngestTerminalStatus,
    exit_code_for_status,
)
from core.telemetry import METRIC_INGEST_OUTCOME, get_telemetry
from ingest.batch_ingest import WorkerTeardownReport, _derive_run_status
from ingest.document_content import (
    ENCODING_FALLBACK_IGNORE,
    ENCODING_FALLBACK_LATIN1,
    extract_text,
    extract_text_and_chunk_in_process,
    extract_text_csv_by_building_enhanced,
    extract_text_with_provenance,
)
from ingest.document_processor import (
    DocumentProcessor,
    Extractor,
    Vectoriser,
    _first_encoding_fallback,
)
from ingest.transaction import ThreadSafeStats, _record_ingested_files
from interfaces.ingest_file_registry import RedisIngestFileRegistry


@pytest.fixture(autouse=True)
def _reset_telemetry():
    get_telemetry().reset()
    yield
    get_telemetry().reset()


# --- decoding provenance ---------------------------------------------------


def test_txt_utf8_failure_reports_latin1_fallback():
    # 0xE9 ("é" in Latin-1) is not a valid stand-alone UTF-8 byte.
    extraction = extract_text_with_provenance("note.txt", "Café".encode("latin-1"))

    assert extraction.encoding_fallback == ENCODING_FALLBACK_LATIN1
    assert extraction.text  # text is still recovered, just possibly lossy


def test_clean_utf8_text_reports_no_fallback():
    extraction = extract_text_with_provenance("note.txt", "hello".encode("utf-8"))

    assert extraction.text == "hello"
    assert extraction.encoding_fallback is None


def test_invalid_json_bytes_report_ignore_fallback():
    extraction = extract_text_with_provenance("data.json", b'{"a": "\xff"}')

    assert extraction.encoding_fallback == ENCODING_FALLBACK_IGNORE


def test_extract_text_wrapper_still_returns_plain_string():
    result = extract_text("note.txt", "Café".encode("latin-1"))

    assert isinstance(result, str)


def test_process_pool_extraction_carries_fallback_reason():
    text, _chunks, fallback = extract_text_and_chunk_in_process(
        "note.txt",
        "Café".encode("latin-1"),
        embed_model="text-embedding-3-small",
        chunk_tokens=100,
        chunk_overlap=10,
    )

    assert text
    assert fallback == ENCODING_FALLBACK_LATIN1


def test_csv_without_property_column_records_encoding_fallback():
    docs = extract_text_csv_by_building_enhanced("weird.csv", b"col1,col2\n1,2\n", {})

    assert docs[0][3]["encoding_fallback"] == ENCODING_FALLBACK_IGNORE


def test_first_encoding_fallback_scans_doc_metadata():
    docs = [
        ("k1", None, "t", {}),
        ("k2", None, "t", {"encoding_fallback": ENCODING_FALLBACK_IGNORE}),
    ]

    assert _first_encoding_fallback(docs) == ENCODING_FALLBACK_IGNORE
    assert _first_encoding_fallback([("k", None, "t", {})]) is None


# --- processor wiring ------------------------------------------------------


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
    def resolve(self, _key, _text):  # noqa: D401 - test stub
        return _Resolution()


def _make_processor() -> DocumentProcessor:
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            embed_model="text-embedding-3-small",
            chunk_tokens=100,
            chunk_overlap=10,
            dry_run=False,
        ),
        logger=logging.getLogger("ingest-06-test"),
        stats=ThreadSafeStats(),
        encoder=_FakeEncoder(),
    )
    return DocumentProcessor(
        ctx=ctx,
        base_path="/data",
        alias_to_canonical={},
        fra_vector_extractor=lambda *a, **k: {},
        building_resolver=_FakeResolver(),
    )


def test_extractor_surfaces_encoding_fallback_for_lossy_text():
    processor = _make_processor()
    extractor = Extractor(processor)

    result = extractor.extract(
        key="note.txt", extension="txt", data="Café".encode("latin-1")
    )

    # 6-tuple: (docs, text_sample, building, is_fra_candidate, chunks, fallback)
    assert result[5] == ENCODING_FALLBACK_LATIN1


def test_extractor_reports_no_fallback_for_clean_text():
    processor = _make_processor()
    extractor = Extractor(processor)

    result = extractor.extract(
        key="note.txt", extension="txt", data="clean text".encode("utf-8")
    )

    assert result[5] is None


def test_partial_outcome_outranks_degraded():
    processor = _make_processor()

    processor.note_file_outcome("file", "degraded", ENCODING_FALLBACK_LATIN1)
    processor.note_file_outcome("file", "partial", "embedding_partial")

    assert processor.take_file_outcome("file") == ("partial", "embedding_partial")


def test_needs_review_outranks_degraded():
    processor = _make_processor()

    processor.note_file_outcome("file", "degraded", ENCODING_FALLBACK_LATIN1)
    processor.note_file_outcome("file", "needs_review", "fra_no_action_plan")

    assert processor.take_file_outcome("file")[0] == "needs_review"


def test_degraded_survives_as_sole_outcome():
    processor = _make_processor()

    processor.note_file_outcome("file", "degraded", ENCODING_FALLBACK_LATIN1)

    assert processor.take_file_outcome("file") == (
        "degraded",
        ENCODING_FALLBACK_LATIN1,
    )


def _vector(vector_id: str) -> dict:
    return {
        "id": vector_id,
        "values": [0.1],
        "metadata": {"source": "note.txt", "source_path": "/data"},
        "namespace": "docs",
        "_processing_token": "token",
    }


def test_degraded_note_becomes_file_terminal_status_on_vectors():
    processor = _make_processor()
    processor.handle_dry_run = lambda *a, **k: False
    processor.maybe_extract_fra_vectors = lambda **k: False
    processor.build_vectors_from_docs = lambda **k: [_vector("file:doc:0")]

    # Extraction recorded a lossy decode before vectorisation runs.
    processor.note_file_outcome("file", "degraded", ENCODING_FALLBACK_LATIN1)

    vectors = Vectoriser(processor).vectorise(
        key="note.txt",
        extension="txt",
        file_id="file",
        content_hash="abc",
        processing_token="token",
        start_time=0.0,
        docs=[("note.txt", "Test Building", "text", {})],
        text_sample="text",
        building="Test Building",
        is_fra_candidate=False,
        precomputed_chunks=None,
    )

    assert vectors
    assert all(v["_file_terminal_status"] == "degraded" for v in vectors)
    assert vectors[0]["_file_terminal_reason"] == ENCODING_FALLBACK_LATIN1


# --- terminal-state contract ----------------------------------------------


def test_degraded_override_finishes_file_registry_degraded():
    registry = Mock()
    ctx = SimpleNamespace(
        config=SimpleNamespace(dry_run=False),
        file_registry=registry,
        stats=ThreadSafeStats(),
    )
    vector = {
        **_vector("file:doc:0"),
        "_file_terminal_status": "degraded",
        "_file_terminal_reason": ENCODING_FALLBACK_LATIN1,
    }

    _record_ingested_files(ctx, [vector], status="success")

    record = registry.upsert_with_token.call_args.args[0]
    assert record.status == "degraded"
    assert record.error == ENCODING_FALLBACK_LATIN1
    assert ctx.stats.get_stats()["file_terminal_states"]["note.txt"] == "degraded"
    assert (
        get_telemetry().get(METRIC_INGEST_OUTCOME, scope="file", status="degraded")
        == 1
    )


def test_success_cannot_overwrite_degraded_terminal_state():
    stats = ThreadSafeStats()

    stats.record_file_terminal("a.txt", "degraded")
    stats.record_file_terminal("a.txt", "success")

    assert stats.get_stats()["file_terminal_states"]["a.txt"] == "degraded"


def test_degraded_cannot_overwrite_partial_terminal_state():
    stats = ThreadSafeStats()

    stats.record_file_terminal("a.txt", "partial")
    stats.record_file_terminal("a.txt", "degraded")

    assert stats.get_stats()["file_terminal_states"]["a.txt"] == "partial"


def test_mark_state_accepts_degraded_status():
    class _Script:
        def __call__(self, **_kwargs):
            return 1

    class _Client:
        def register_script(self, _source):
            return _Script()

    registry = RedisIngestFileRegistry(_Client())

    # Must not raise "Unknown ingest file state".
    registry.mark_state(
        file_id="f",
        processing_token="t",
        status="degraded",
        source_path="/data",
        source_key="a.txt",
        content_hash="h",
    )


# --- run aggregation + exit codes -----------------------------------------


def _ctx():
    return SimpleNamespace(config=SimpleNamespace(dry_run=False), stats=ThreadSafeStats())


@pytest.mark.parametrize(
    "file_outcomes, expected",
    [
        ({"a": "degraded"}, IngestTerminalStatus.DEGRADED),
        ({"a": "success", "b": "degraded"}, IngestTerminalStatus.DEGRADED),
        ({"a": "degraded", "b": "partial"}, IngestTerminalStatus.PARTIAL),
        ({"a": "degraded", "b": "failed"}, IngestTerminalStatus.PARTIAL),
    ],
)
def test_run_status_reflects_degraded_files(file_outcomes, expected):
    status = _derive_run_status(
        ctx=_ctx(),
        files_found=len(file_outcomes),
        file_outcomes=file_outcomes,
        upsert_errors=[],
        teardown=WorkerTeardownReport(),
    )

    assert status is expected


def test_degraded_run_maps_to_success_exit_code():
    assert exit_code_for_status(IngestTerminalStatus.DEGRADED) == IngestExitCode.SUCCESS
