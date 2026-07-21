"""Audit and quarantine vectors missing the required ACL envelope (AUTH-10).

The workflow is deliberately audit-only by default. Quarantine must be selected
explicitly by an operator, and every run produces a durable report containing
opaque vector references rather than vector IDs, source paths, or document keys.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator

from auth.access_control import has_required_acl_metadata
from config import ACL_CONFORMANCE_THRESHOLD, NAMESPACE_MAPPINGS, normalise_ns
from core.ingest_outcomes import IngestTerminalStatus, exit_code_for_status
from core.telemetry import get_telemetry
from security.log_sanitiser import sanitise_error

DEFAULT_FETCH_BATCH_SIZE = 100
DEFAULT_REPORT_PATH = "logs/acl_reconciliation.json"


class AclRemediationAction(str, Enum):
    """Operator-selected behavior for non-conformant vectors."""

    AUDIT = "audit"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class AclVectorFinding:
    """Internal finding; raw IDs never leave this process in reports/metrics."""

    vector_id: str
    namespace: str | None

    @property
    def reference(self) -> str:
        scope = self.namespace or "__default__"
        digest = hashlib.sha256(f"{scope}:{self.vector_id}".encode()).hexdigest()
        return f"acl-{digest[:16]}"


@dataclass(frozen=True)
class AclReconciliationReport:
    """Terminal audit/remediation result suitable for automation."""

    status: IngestTerminalStatus
    action: AclRemediationAction
    threshold: float
    scanned: int
    compliant: int
    nonconformant: int
    remediated: int
    failed: int
    conformance_ratio: float
    meets_threshold: bool
    references: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return int(exit_code_for_status(self.status))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "action": self.action.value,
            "threshold": self.threshold,
            "scanned": self.scanned,
            "compliant": self.compliant,
            "nonconformant": self.nonconformant,
            "remediated": self.remediated,
            "failed": self.failed,
            "conformance_ratio": self.conformance_ratio,
            "meets_threshold": self.meets_threshold,
            "references": list(self.references),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }


def configured_acl_namespaces() -> tuple[str | None, ...]:
    """Return the unique namespaces written by current ingestion routing."""

    # Include the default namespace because legacy vectors can predate explicit
    # routing and are exactly the population this reconciliation targets.
    return (None, *sorted(set(NAMESPACE_MAPPINGS.values())))


def _iter_ids(pages: Iterable[Any]) -> Iterator[str]:
    for page in pages:
        if isinstance(page, str):
            yield page
            continue
        if isinstance(page, dict):
            page = page.get("ids") or page.get("vectors") or []
        elif hasattr(page, "ids"):
            page = page.ids
        for item in page or []:
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict) and item.get("id"):
                yield str(item["id"])
            elif getattr(item, "id", None):
                yield str(item.id)


def _response_vectors(response: Any) -> dict[str, Any]:
    vectors = response.get("vectors") if isinstance(response, dict) else None
    if vectors is None:
        vectors = getattr(response, "vectors", None)
    return vectors if isinstance(vectors, dict) else {}


def _metadata(vector: Any) -> dict[str, Any]:
    metadata = vector.get("metadata") if isinstance(vector, dict) else None
    if metadata is None:
        metadata = getattr(vector, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _batched(values: Iterable[str], size: int) -> Iterator[list[str]]:
    batch: list[str] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def scan_acl_conformance(
    vector_store: Any,
    namespaces: Iterable[str | None],
    *,
    fetch_batch_size: int = DEFAULT_FETCH_BATCH_SIZE,
) -> tuple[int, list[AclVectorFinding]]:
    """Scan vector metadata in bounded fetches and return missing-ACL findings."""

    if fetch_batch_size < 1:
        raise ValueError("fetch_batch_size must be positive")
    scanned = 0
    findings: list[AclVectorFinding] = []
    for namespace in namespaces:
        ids = _iter_ids(vector_store.list(namespace=normalise_ns(namespace)))
        for batch in _batched(ids, fetch_batch_size):
            vectors = _response_vectors(
                vector_store.fetch(ids=batch, namespace=normalise_ns(namespace))
            )
            for vector_id in batch:
                vector = vectors.get(vector_id)
                if vector is None:
                    continue
                scanned += 1
                if not has_required_acl_metadata(_metadata(vector)):
                    findings.append(AclVectorFinding(vector_id, namespace))
    return scanned, findings


def _quarantine(
    vector_store: Any,
    findings: list[AclVectorFinding],
    *,
    batch_size: int,
    logger: logging.Logger,
) -> tuple[int, int]:
    grouped: dict[str | None, list[str]] = {}
    for finding in findings:
        grouped.setdefault(finding.namespace, []).append(finding.vector_id)

    remediated = 0
    failed = 0
    for namespace, vector_ids in grouped.items():
        for batch in _batched(vector_ids, batch_size):
            try:
                vector_store.delete(ids=batch, namespace=normalise_ns(namespace))
                remediated += len(batch)
            except Exception as error:  # pylint: disable=broad-except
                failed += len(batch)
                logger.error(
                    "acl_quarantine_failed namespace=%s count=%d error=%s",
                    namespace or "__default__",
                    len(batch),
                    sanitise_error(error),
                    exc_info=False,
                )
    return remediated, failed


def _write_report(report: AclReconciliationReport, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def reconcile_acl_vectors(
    ctx: Any,
    *,
    action: AclRemediationAction | str = AclRemediationAction.AUDIT,
    namespaces: Iterable[str | None] | None = None,
    threshold: float = ACL_CONFORMANCE_THRESHOLD,
    fetch_batch_size: int = DEFAULT_FETCH_BATCH_SIZE,
    report_path: str = DEFAULT_REPORT_PATH,
) -> AclReconciliationReport:
    """Audit ACL conformance and optionally quarantine every finding."""

    stable_action = AclRemediationAction(action)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0.0 and 1.0")
    selected_namespaces = tuple(
        configured_acl_namespaces() if namespaces is None else namespaces
    )
    initial_scanned, findings = scan_acl_conformance(
        ctx.vector_store,
        selected_namespaces,
        fetch_batch_size=fetch_batch_size,
    )
    telemetry = get_telemetry()
    initial_compliant = initial_scanned - len(findings)
    telemetry.record_acl_reconciliation(
        stable_action.value, "compliant", initial_compliant
    )
    telemetry.record_acl_reconciliation(
        stable_action.value, "nonconformant", len(findings)
    )

    remediated = 0
    failed = 0
    final_scanned = initial_scanned
    final_nonconformant = len(findings)
    if stable_action is AclRemediationAction.QUARANTINE and findings:
        remediated, failed = _quarantine(
            ctx.vector_store,
            findings,
            batch_size=fetch_batch_size,
            logger=getattr(ctx, "logger", logging.getLogger(__name__)),
        )
        telemetry.record_acl_reconciliation(
            stable_action.value, "remediated", remediated
        )
        telemetry.record_acl_reconciliation(stable_action.value, "failed", failed)
        final_scanned, remaining = scan_acl_conformance(
            ctx.vector_store,
            selected_namespaces,
            fetch_batch_size=fetch_batch_size,
        )
        final_nonconformant = len(remaining)

    final_compliant = final_scanned - final_nonconformant
    ratio = final_compliant / final_scanned if final_scanned else 1.0
    meets_threshold = ratio >= threshold
    if stable_action is AclRemediationAction.AUDIT:
        status = (
            IngestTerminalStatus.SUCCESS
            if meets_threshold
            else IngestTerminalStatus.NEEDS_REVIEW
        )
    else:
        status = (
            IngestTerminalStatus.SUCCESS
            if failed == 0 and meets_threshold
            else IngestTerminalStatus.PARTIAL
        )

    report = AclReconciliationReport(
        status=status,
        action=stable_action,
        threshold=threshold,
        scanned=initial_scanned,
        compliant=initial_compliant,
        nonconformant=len(findings),
        remediated=remediated,
        failed=failed,
        conformance_ratio=ratio,
        meets_threshold=meets_threshold,
        references=tuple(finding.reference for finding in findings),
    )
    if report_path:
        _write_report(report, report_path)
    return report


__all__ = [
    "AclReconciliationReport",
    "AclRemediationAction",
    "AclVectorFinding",
    "configured_acl_namespaces",
    "reconcile_acl_vectors",
    "scan_acl_conformance",
]
