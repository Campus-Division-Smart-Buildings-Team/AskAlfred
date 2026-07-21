"""Behavioral coverage for AUTH-10 ACL-vector reconciliation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.ingest_outcomes import IngestTerminalStatus
from core.telemetry import METRIC_ACL_RECONCILIATION, get_telemetry
from ingest.acl_reconciliation import (
    AclRemediationAction,
    reconcile_acl_vectors,
    scan_acl_conformance,
)


class FakeVectorStore:
    def __init__(self, vectors_by_namespace):
        self.vectors_by_namespace = vectors_by_namespace
        self.deleted: list[tuple[str | None, tuple[str, ...]]] = []
        self.fail_delete = False

    def list(self, namespace=None):
        ids = list(self.vectors_by_namespace.get(namespace, {}))
        return [ids[:2], ids[2:]]

    def fetch(self, ids, namespace=None):
        vectors = self.vectors_by_namespace.get(namespace, {})
        return {"vectors": {vector_id: vectors[vector_id] for vector_id in ids if vector_id in vectors}}

    def delete(self, ids, namespace=None):
        if self.fail_delete:
            raise RuntimeError("backend detail must not enter the report")
        self.deleted.append((namespace, tuple(ids)))
        vectors = self.vectors_by_namespace.get(namespace, {})
        for vector_id in ids:
            vectors.pop(vector_id, None)


def _compliant(source="safe-source"):
    return {
        "metadata": {
            "tenant_id": "tenant-a",
            "access_level": "pilot_internal",
            "allowed_roles": ["base_view"],
            "source": source,
        }
    }


def _nonconformant(source="private/source.pdf"):
    return {"metadata": {"tenant_id": "tenant-a", "source": source}}


@pytest.fixture(autouse=True)
def _reset_telemetry():
    get_telemetry().reset()
    yield
    get_telemetry().reset()


def test_audit_identifies_findings_without_exposing_vector_or_source(tmp_path):
    store = FakeVectorStore(
        {"operational_docs": {"vector-safe": _compliant(), "vector-secret": _nonconformant()}}
    )
    report_path = tmp_path / "acl-report.json"

    report = reconcile_acl_vectors(
        SimpleNamespace(vector_store=store),
        namespaces=["operational_docs"],
        threshold=1.0,
        report_path=str(report_path),
    )

    assert report.status is IngestTerminalStatus.NEEDS_REVIEW
    assert report.action is AclRemediationAction.AUDIT
    assert report.scanned == 2
    assert report.compliant == 1
    assert report.nonconformant == 1
    assert report.remediated == 0
    assert report.conformance_ratio == 0.5
    assert report.meets_threshold is False
    assert store.deleted == []
    assert get_telemetry().get(
        METRIC_ACL_RECONCILIATION, action="audit", state="nonconformant"
    ) == 1

    raw_report = report_path.read_text(encoding="utf-8")
    payload = json.loads(raw_report)
    assert payload["references"] == list(report.references)
    assert report.references[0].startswith("acl-")
    assert "vector-secret" not in raw_report
    assert "private/source.pdf" not in raw_report


def test_quarantine_removes_every_finding_and_verifies_threshold(tmp_path):
    store = FakeVectorStore(
        {
            "operational_docs": {
                "safe": _compliant(),
                "bad-1": _nonconformant(),
                "bad-2": _nonconformant(),
            }
        }
    )

    report = reconcile_acl_vectors(
        SimpleNamespace(vector_store=store),
        action="quarantine",
        namespaces=["operational_docs"],
        threshold=1.0,
        fetch_batch_size=1,
        report_path=str(tmp_path / "acl-report.json"),
    )

    assert report.status is IngestTerminalStatus.SUCCESS
    assert report.remediated == 2
    assert report.failed == 0
    assert report.conformance_ratio == 1.0
    assert report.meets_threshold is True
    assert store.deleted == [
        ("operational_docs", ("bad-1",)),
        ("operational_docs", ("bad-2",)),
    ]
    assert get_telemetry().get(
        METRIC_ACL_RECONCILIATION, action="quarantine", state="remediated"
    ) == 2


def test_quarantine_failure_is_partial_and_retains_privacy(tmp_path):
    store = FakeVectorStore({"operational_docs": {"secret-id": _nonconformant()}})
    store.fail_delete = True
    report_path = tmp_path / "acl-report.json"

    report = reconcile_acl_vectors(
        SimpleNamespace(vector_store=store),
        action="quarantine",
        namespaces=["operational_docs"],
        report_path=str(report_path),
    )

    assert report.status is IngestTerminalStatus.PARTIAL
    assert report.remediated == 0
    assert report.failed == 1
    assert report.meets_threshold is False
    assert "secret-id" not in report_path.read_text(encoding="utf-8")
    assert "backend detail" not in report_path.read_text(encoding="utf-8")


def test_scan_uses_bounded_fetches_and_ignores_disappeared_vectors():
    store = FakeVectorStore(
        {"operational_docs": {"one": _compliant(), "two": _nonconformant()}}
    )
    original_fetch = store.fetch

    def fetch_with_disappeared_vector(ids, namespace=None):
        response = original_fetch(ids, namespace)
        response["vectors"].pop("two", None)
        return response

    store.fetch = fetch_with_disappeared_vector

    scanned, findings = scan_acl_conformance(
        store, ["operational_docs"], fetch_batch_size=1
    )

    assert scanned == 1
    assert findings == []


@pytest.mark.parametrize("threshold", [-0.01, 1.01])
def test_invalid_deployment_threshold_is_rejected(threshold, tmp_path):
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        reconcile_acl_vectors(
            SimpleNamespace(vector_store=FakeVectorStore({})),
            threshold=threshold,
            report_path=str(tmp_path / "unused.json"),
        )
