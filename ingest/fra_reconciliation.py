"""Idempotent recovery for open FRA supersession journal records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from building.normaliser import normalise_building_name
from config import FRA_LOCK_TIMEOUT_SECONDS, FRA_RISK_ITEMS_NAMESPACE
from core.ingest_outcomes import IngestTerminalStatus, exit_code_for_status
from core.telemetry import get_telemetry
from fra import restore_superseded_items
from interfaces import FraJournalRecord, FraJournalState, JobRecord

from .transaction import (
    FraVerificationState,
    _acquire_fra_locks,
    _verify_fra_vectors_present,
    _verify_restored_fra_items,
)


@dataclass(frozen=True)
class FraReconciliationReport:
    status: IngestTerminalStatus
    examined: int
    reconciled: int
    remaining: int
    transaction_ids: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return int(exit_code_for_status(self.status))


def _finalise_jobs(ctx, record: FraJournalRecord, status: str) -> None:
    now = datetime.now(timezone.utc).isoformat() + "Z"
    for building, assessment_date in record.requests:
        registry_id = (
            f"fra_supersede:{normalise_building_name(building)}:{assessment_date}"
        )
        ctx.job_registry.upsert(
            JobRecord(
                job_id=registry_id,
                job_type="fra_supersession",
                status=status,
                started_at_iso=record.created_at_iso,
                finished_at_iso=now,
                error=None if status == "success" else "transaction_reconciled",
                meta={
                    "building": building,
                    "assessment_date": assessment_date,
                    "tx_id": record.tx_id,
                    "reconciled": True,
                },
            )
        )


def _reconcile_one(ctx, record: FraJournalRecord) -> bool:
    with _acquire_fra_locks(
        ctx,
        list(record.buildings),
        timeout_seconds=FRA_LOCK_TIMEOUT_SECONDS,
    ):
        current = ctx.fra_journal.get(record.tx_id)
        if current is None:
            return True
        if current.state in {FraJournalState.COMMITTED, FraJournalState.ROLLED_BACK}:
            ctx.fra_journal.unblock_buildings(
                current.tx_id, list(current.buildings)
            )
            return True

        if current.state is FraJournalState.VERIFICATION_UNAVAILABLE:
            vectors = [
                {"id": vector_id, "namespace": FRA_RISK_ITEMS_NAMESPACE}
                for vector_id in current.vector_ids
            ]
            verification = _verify_fra_vectors_present(ctx, vectors, attempts=1)
            if verification.state is FraVerificationState.UNAVAILABLE:
                return False
            if verification.state is FraVerificationState.PRESENT:
                _finalise_jobs(ctx, current, "success")
                ctx.fra_journal.transition(
                    current.tx_id, FraJournalState.COMMITTED
                )
                ctx.fra_journal.unblock_buildings(
                    current.tx_id, list(current.buildings)
                )
                ctx.stats.increment("fra_reconciliations_total")
                get_telemetry().record_ingest_integrity(
                    "reconciliation", "committed"
                )
                return True

        item_ids = list(current.superseded_ids)
        ctx.fra_journal.transition(
            current.tx_id,
            FraJournalState.ROLLBACK_PENDING,
            failure_code="fra.reconciliation_required",
        )
        restored = restore_superseded_items(ctx, item_ids)
        verified = _verify_restored_fra_items(ctx, item_ids)
        if restored != len(item_ids) or not verified:
            ctx.fra_journal.transition(
                current.tx_id,
                FraJournalState.CRITICAL_INCONSISTENT,
                failure_code="fra.rollback_failed",
            )
            ctx.fra_journal.block_buildings(
                current.tx_id, list(current.buildings)
            )
            ctx.stats.increment("critical_inconsistent_total")
            get_telemetry().record_ingest_integrity(
                "reconciliation", "critical_inconsistent"
            )
            return False

        _finalise_jobs(ctx, current, "failed")
        ctx.fra_journal.transition(current.tx_id, FraJournalState.ROLLED_BACK)
        ctx.fra_journal.unblock_buildings(current.tx_id, list(current.buildings))
        ctx.stats.increment("fra_reconciliations_total")
        get_telemetry().record_ingest_integrity("reconciliation", "rolled_back")
        return True


def reconcile_fra_transactions(
    ctx, *, transaction_id: str | None = None
) -> FraReconciliationReport:
    """Recover one or all open transactions; safe to call repeatedly."""

    if transaction_id:
        record = ctx.fra_journal.get(transaction_id)
        records = [record] if record is not None else []
    else:
        records = ctx.fra_journal.list_open()

    reconciled = 0
    remaining_ids: list[str] = []
    for record in records:
        try:
            if _reconcile_one(ctx, record):
                reconciled += 1
            else:
                remaining_ids.append(record.tx_id)
        except Exception as error:  # pylint: disable=broad-except
            ctx.logger.error(
                "FRA reconciliation failed for transaction %s: %s",
                record.tx_id,
                error,
            )
            remaining_ids.append(record.tx_id)

    if remaining_ids:
        status = IngestTerminalStatus.CRITICAL_INCONSISTENT
    else:
        status = IngestTerminalStatus.SUCCESS
    return FraReconciliationReport(
        status=status,
        examined=len(records),
        reconciled=reconciled,
        remaining=len(remaining_ids),
        transaction_ids=tuple(remaining_ids),
    )


__all__ = ["FraReconciliationReport", "reconcile_fra_transactions"]
