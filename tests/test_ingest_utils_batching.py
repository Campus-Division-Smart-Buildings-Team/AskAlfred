#!/usr/bin/env python3
"""Tests for metadata-aware Pinecone batch sizing."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

from ingest.utils import calculate_max_batch_size, upsert_vectors

_MB = 1024 * 1024


def test_max_batch_size_without_metadata_matches_dimension_budget():
    assert calculate_max_batch_size(1536, per_vector_overhead_bytes=0) == _MB // (
        1536 * 4
    )


def test_metadata_shrinks_max_batch_size():
    plain = calculate_max_batch_size(1536)
    heavy = calculate_max_batch_size(1536, metadata_bytes_per_vector=10_240)
    assert heavy < plain
    assert heavy == _MB // (1536 * 4 + 10_240 + 128)


def test_max_batch_size_never_below_one():
    assert calculate_max_batch_size(1536, metadata_bytes_per_vector=10 * _MB) == 1


def test_negative_metadata_estimate_is_ignored():
    assert calculate_max_batch_size(
        1536, metadata_bytes_per_vector=-500
    ) == calculate_max_batch_size(1536)


class FakeStore:
    def __init__(self):
        self.calls = []

    def upsert(self, vectors, namespace=None):
        self.calls.append((namespace, vectors))


def _make_ctx(store, dimension=8):
    return SimpleNamespace(
        vector_store=store,
        logger=Mock(),
        stats=Mock(),
        config=SimpleNamespace(dimension=dimension),
    )


def test_upsert_vectors_slices_requests_by_metadata_size():
    store = FakeStore()
    ctx = _make_ctx(store)
    # Identical fat metadata on every vector so the sampling estimate is exact.
    vectors = [
        {
            "id": f"v:{i}",
            "values": [0.0] * 8,
            "metadata": {"text": "x" * 8000},
            "namespace": "ns1",
        }
        for i in range(300)
    ]

    upsert_vectors(ctx, vectors)

    # Dimension-only sizing (8 * 4 bytes/vector) would send all 300 in one call.
    assert len(store.calls) >= 2
    assert sum(len(vecs) for _, vecs in store.calls) == 300
    assert all(ns == "ns1" for ns, _ in store.calls)
    # Each request payload stays within the ~1MB budget (plus slack for the
    # sampling estimate and JSON framing).
    for _, vecs in store.calls:
        assert len(json.dumps(vecs)) < 1.2 * _MB

    sent_ids = [v["id"] for _, vecs in store.calls for v in vecs]
    assert sent_ids == [f"v:{i}" for i in range(300)]


def test_upsert_vectors_small_metadata_uses_single_request():
    store = FakeStore()
    ctx = _make_ctx(store)
    vectors = [
        {
            "id": f"v:{i}",
            "values": [0.0] * 8,
            "metadata": {"key": "small"},
            "namespace": "ns1",
        }
        for i in range(50)
    ]

    upsert_vectors(ctx, vectors)

    assert len(store.calls) == 1
    assert len(store.calls[0][1]) == 50
