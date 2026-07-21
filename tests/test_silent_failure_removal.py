"""Behavioural regression tests for the retired silent-failure baseline."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.alfred_exceptions import ParseError, SearchError, StructuredSearchUnavailable
from core.failure_codes import FailureCode
from core.telemetry import METRIC_FALLBACK_ACTIVATED, get_telemetry


def test_pinecone_catalog_failure_is_not_an_empty_catalogue(monkeypatch):
    from core import pinecone_utils

    client = Mock()
    client.list_indexes.side_effect = ConnectionError("catalogue unavailable")
    monkeypatch.setattr(pinecone_utils, "get_pc", lambda: client)

    with pytest.raises(ConnectionError, match="catalogue unavailable"):
        pinecone_utils.list_index_names()


def test_query_all_chunks_failure_is_not_an_empty_result():
    from core.pinecone_utils import query_all_chunks

    index = Mock()
    index.query.side_effect = TimeoutError("query unavailable")

    with pytest.raises(TimeoutError, match="query unavailable"):
        query_all_chunks(index, None, [0.0])


def test_query_all_chunks_rejects_invalid_provider_contract():
    from core.pinecone_utils import query_all_chunks

    index = Mock()
    index.query.return_value = {"unexpected": []}

    with pytest.raises(SearchError, match="invalid response contract"):
        query_all_chunks(index, None, [0.0])


def test_structured_list_wrapper_raises_typed_backend_failure(monkeypatch):
    from search_core import structured_queries

    monkeypatch.setattr(
        structured_queries,
        "_fetch_index_matches",
        Mock(side_effect=ConnectionError("provider detail")),
    )

    with pytest.raises(StructuredSearchUnavailable) as caught:
        structured_queries._query_index_with_batches("example-index", None)

    failure = caught.value.failure
    assert failure.code is FailureCode.STRUCTURED_SEARCH_UNAVAILABLE
    assert failure.retryable is True
    assert failure.safe_context == {"phase": "index_query"}


def test_document_chunk_fallback_failure_raises_and_emits_telemetry(monkeypatch):
    from core import date_utils

    get_telemetry().reset()
    index = Mock()
    index.query.side_effect = ConnectionError("metadata query unavailable")
    monkeypatch.setattr(
        date_utils,
        "vector_query",
        Mock(side_effect=TimeoutError("semantic query unavailable")),
    )

    with pytest.raises(SearchError, match="Document chunk retrieval unavailable"):
        date_utils._fetch_document_chunks(index, "document-key", None)

    assert get_telemetry().get(
        METRIC_FALLBACK_ACTIVATED, component="date_lookup"
    ) == 1
    get_telemetry().reset()


def test_maintenance_csv_parse_failure_is_an_explicit_file_error(monkeypatch):
    from ingest import document_content

    monkeypatch.setattr(
        document_content,
        "load_tabular_data",
        Mock(side_effect=ValueError("malformed workbook")),
    )

    with pytest.raises(ParseError, match="Maintenance CSV extraction failed"):
        document_content.extract_maintenance_csv("Maintenance Jobs.csv", b"bad", {})


def test_alias_override_validation_failure_propagates(monkeypatch):
    from building import alias_override

    monkeypatch.setattr(
        alias_override.pd,
        "read_csv",
        Mock(side_effect=OSError("property catalogue unreadable")),
    )

    with pytest.raises(OSError, match="property catalogue unreadable"):
        alias_override.validate_overrides("missing.csv")


def test_binary_probe_failure_propagates_for_explicit_file_treatment(tmp_path):
    from building.path_inventory_summary import _is_binary_file

    with pytest.raises(FileNotFoundError):
        _is_binary_file(tmp_path / "missing.txt")


def test_inline_upsert_terminal_failure_is_marked_and_raised(monkeypatch):
    from ingest import upsert_handler

    ctx = SimpleNamespace(
        config=SimpleNamespace(dry_run=False),
        stats=Mock(),
        logger=logging.getLogger("inline-upsert-test"),
        event_sink=Mock(),
    )
    terminal_marker = Mock()
    monkeypatch.setattr(
        upsert_handler.UpsertExecutor,
        "execute_once",
        Mock(side_effect=ValueError("invalid vector")),
    )
    monkeypatch.setattr(upsert_handler, "_mark_batch_failed", terminal_marker)
    dispatcher = upsert_handler.Dispatcher(
        ctx,
        writer=Mock(),
        use_worker=False,
        upsert_queue=None,
    )
    batch = [{"id": "file:chunk", "values": [0.0], "metadata": {}}]

    with pytest.raises(ValueError, match="invalid vector"):
        dispatcher.submit(batch)

    terminal_marker.assert_called_once_with(ctx, batch, reason="upsert_failed")
    ctx.stats.increment.assert_any_call("upsert_batch_failures_total")
