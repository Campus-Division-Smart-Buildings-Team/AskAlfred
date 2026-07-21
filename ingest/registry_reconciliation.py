"""Durable local spool for vector-success/file-registry divergence."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.ingest_outcomes import IngestTerminalStatus, exit_code_for_status


@dataclass(frozen=True)
class RegistryReconciliationReport:
    status: IngestTerminalStatus
    examined: int
    reconciled: int
    remaining: int

    @property
    def exit_code(self) -> int:
        return int(exit_code_for_status(self.status))


def _spool_path(ctx) -> Path:
    configured = getattr(ctx.config, "registry_reconciliation_file", "")
    return Path(configured or "logs/ingest_registry_reconciliation.jsonl")


def spool_registry_divergence(ctx, vectors: list[dict[str, Any]]) -> int:
    """Append one replay-safe file transition per affected source file."""

    records: dict[str, dict[str, Any]] = {}
    for vector in vectors:
        vector_id = vector.get("id")
        if not vector_id:
            continue
        file_id = str(vector_id).split(":", 1)[0]
        metadata = vector.get("metadata") or {}
        record = records.setdefault(
            file_id,
            {
                "record_id": uuid.uuid4().hex,
                "file_id": file_id,
                "source_path": metadata.get("source_path", ""),
                "source_key": metadata.get("source") or metadata.get("key") or "",
                "content_hash": metadata.get("content_hash"),
                "processing_token": vector.get("_processing_token"),
                "namespaces": [],
                "created_at_iso": datetime.now(timezone.utc).isoformat() + "Z",
            },
        )
        namespace = vector.get("namespace")
        if namespace and namespace not in record["namespaces"]:
            record["namespaces"].append(namespace)

    if not records:
        return 0
    path = _spool_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = getattr(ctx, "export_events_lock", None)
    if lock is not None:
        lock.acquire()
    try:
        with path.open("a", encoding="utf-8") as handle:
            for record in records.values():
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
    finally:
        if lock is not None:
            lock.release()
    return len(records)


def reconcile_registry_divergence(ctx) -> RegistryReconciliationReport:
    """Replay spooled registry transitions and retain only unresolved entries."""

    path = _spool_path(ctx)
    if not path.exists():
        return RegistryReconciliationReport(
            IngestTerminalStatus.SUCCESS, 0, 0, 0
        )
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            entries.append(payload)

    remaining: list[dict[str, Any]] = []
    reconciled = 0
    for entry in entries:
        try:
            ctx.file_registry.mark_state(
                file_id=str(entry["file_id"]),
                processing_token=entry.get("processing_token"),
                status="partial",
                error="registry_reconciled_after_vector_success",
                source_path=str(entry.get("source_path") or ""),
                source_key=str(entry.get("source_key") or ""),
                content_hash=entry.get("content_hash"),
                namespaces=tuple(str(value) for value in entry.get("namespaces", [])),
            )
            reconciled += 1
        except Exception:  # pylint: disable=broad-except
            remaining.append(entry)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in remaining),
        encoding="utf-8",
    )
    temporary.replace(path)
    status = (
        IngestTerminalStatus.PARTIAL
        if remaining
        else IngestTerminalStatus.SUCCESS
    )
    return RegistryReconciliationReport(
        status=status,
        examined=len(entries),
        reconciled=reconciled,
        remaining=len(remaining),
    )


__all__ = [
    "RegistryReconciliationReport",
    "reconcile_registry_divergence",
    "spool_registry_divergence",
]
