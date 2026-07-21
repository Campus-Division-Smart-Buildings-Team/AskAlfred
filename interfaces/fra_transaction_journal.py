"""Durable journal port for FRA supersession transactions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from typing import Protocol, cast

from redis import Redis


class FraJournalState(str, Enum):
    PREPARED = "prepared"
    SUPERSESSION_PLANNED = "supersession_planned"
    SUPERSEDED = "superseded"
    UPSERTED = "upserted"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"
    VERIFIED = "verified"
    COMMITTED = "committed"
    ROLLBACK_PENDING = "rollback_pending"
    ROLLED_BACK = "rolled_back"
    CRITICAL_INCONSISTENT = "critical_inconsistent"


FINAL_JOURNAL_STATES = frozenset(
    {FraJournalState.COMMITTED, FraJournalState.ROLLED_BACK}
)


@dataclass(frozen=True)
class FraJournalRecord:
    tx_id: str
    state: FraJournalState
    buildings: tuple[str, ...]
    requests: tuple[tuple[str, str], ...]
    vector_ids: tuple[str, ...]
    superseded_ids: tuple[str, ...]
    created_at_iso: str
    updated_at_iso: str
    failure_code: str | None = None


class FraTransactionJournal(Protocol):
    def begin(self, record: FraJournalRecord) -> None: ...
    def get(self, tx_id: str) -> FraJournalRecord | None: ...
    def list_open(self) -> list[FraJournalRecord]: ...
    def append_superseded(self, tx_id: str, item_ids: list[str]) -> None: ...
    def transition(
        self,
        tx_id: str,
        state: FraJournalState,
        *,
        failure_code: str | None = None,
    ) -> None: ...
    def block_buildings(self, tx_id: str, buildings: list[str]) -> None: ...
    def unblock_buildings(self, tx_id: str, buildings: list[str]) -> None: ...
    def blocking_transaction(self, building: str) -> str | None: ...


def new_fra_journal_record(
    *,
    tx_id: str,
    buildings: list[str],
    requests: list[tuple[str, str]],
    vector_ids: list[str],
) -> FraJournalRecord:
    now = datetime.now(timezone.utc).isoformat() + "Z"
    return FraJournalRecord(
        tx_id=tx_id,
        state=FraJournalState.PREPARED,
        buildings=tuple(sorted(set(buildings))),
        requests=tuple(requests),
        vector_ids=tuple(dict.fromkeys(vector_ids)),
        superseded_ids=(),
        created_at_iso=now,
        updated_at_iso=now,
    )


def _encode(record: FraJournalRecord) -> str:
    payload = asdict(record)
    payload["state"] = record.state.value
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _decode(raw: object) -> FraJournalRecord | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        return None
    payload = cast(dict[str, object], json.loads(raw))
    return FraJournalRecord(
        tx_id=str(payload["tx_id"]),
        state=FraJournalState(str(payload["state"])),
        buildings=tuple(str(value) for value in payload.get("buildings", [])),
        requests=tuple(
            (str(value[0]), str(value[1]))
            for value in payload.get("requests", [])
            if isinstance(value, list) and len(value) == 2
        ),
        vector_ids=tuple(str(value) for value in payload.get("vector_ids", [])),
        superseded_ids=tuple(
            str(value) for value in payload.get("superseded_ids", [])
        ),
        created_at_iso=str(payload["created_at_iso"]),
        updated_at_iso=str(payload["updated_at_iso"]),
        failure_code=(
            str(payload["failure_code"]) if payload.get("failure_code") else None
        ),
    )


class RedisFraTransactionJournal:
    """Redis-backed immutable-snapshot journal with an open transaction index."""

    def __init__(self, client: Redis, *, prefix: str = "ingest:fra:journal:") -> None:
        self._client = client
        self._prefix = prefix
        self._open_key = f"{prefix}open"
        self._block_prefix = f"{prefix}blocked:"

    def _key(self, tx_id: str) -> str:
        return f"{self._prefix}{tx_id}"

    def _block_key(self, building: str) -> str:
        digest = hashlib.sha256(building.strip().lower().encode("utf-8")).hexdigest()
        return f"{self._block_prefix}{digest}"

    def begin(self, record: FraJournalRecord) -> None:
        pipeline = self._client.pipeline(transaction=True)
        pipeline.set(self._key(record.tx_id), _encode(record), nx=True)
        pipeline.sadd(self._open_key, record.tx_id)
        results = pipeline.execute()
        if not results or not results[0]:
            raise ValueError(f"FRA journal transaction already exists: {record.tx_id}")

    def get(self, tx_id: str) -> FraJournalRecord | None:
        raw = self._client.get(self._key(tx_id))
        return _decode(raw) if raw else None

    def list_open(self) -> list[FraJournalRecord]:
        raw_ids = self._client.smembers(self._open_key) or set()
        records: list[FraJournalRecord] = []
        for raw_id in raw_ids:
            tx_id = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
            record = self.get(tx_id)
            if record is not None and record.state not in FINAL_JOURNAL_STATES:
                records.append(record)
        return sorted(records, key=lambda record: record.created_at_iso)

    def _replace(self, record: FraJournalRecord) -> None:
        self._client.set(self._key(record.tx_id), _encode(record))

    def append_superseded(self, tx_id: str, item_ids: list[str]) -> None:
        record = self.get(tx_id)
        if record is None:
            raise KeyError(f"Unknown FRA journal transaction: {tx_id}")
        combined = tuple(dict.fromkeys((*record.superseded_ids, *item_ids)))
        now = datetime.now(timezone.utc).isoformat() + "Z"
        self._replace(
            replace(
                record,
                superseded_ids=combined,
                state=FraJournalState.SUPERSESSION_PLANNED,
                updated_at_iso=now,
            )
        )

    def transition(
        self,
        tx_id: str,
        state: FraJournalState,
        *,
        failure_code: str | None = None,
    ) -> None:
        record = self.get(tx_id)
        if record is None:
            raise KeyError(f"Unknown FRA journal transaction: {tx_id}")
        now = datetime.now(timezone.utc).isoformat() + "Z"
        self._replace(
            replace(
                record,
                state=state,
                updated_at_iso=now,
                failure_code=failure_code,
            )
        )
        if state in FINAL_JOURNAL_STATES:
            self._client.srem(self._open_key, tx_id)
        else:
            self._client.sadd(self._open_key, tx_id)

    def block_buildings(self, tx_id: str, buildings: list[str]) -> None:
        pipeline = self._client.pipeline(transaction=True)
        for building in buildings:
            pipeline.set(self._block_key(building), tx_id)
        pipeline.execute()

    def unblock_buildings(self, tx_id: str, buildings: list[str]) -> None:
        for building in buildings:
            key = self._block_key(building)
            current = self._client.get(key)
            if isinstance(current, bytes):
                current = current.decode("utf-8")
            if current == tx_id:
                self._client.delete(key)

    def blocking_transaction(self, building: str) -> str | None:
        raw = self._client.get(self._block_key(building))
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return str(raw) if raw else None


class InMemoryFraTransactionJournal:
    """Deterministic test/dry-run implementation of the journal port."""

    def __init__(self) -> None:
        self._records: dict[str, FraJournalRecord] = {}
        self._blocks: dict[str, str] = {}
        self._lock = Lock()

    def begin(self, record: FraJournalRecord) -> None:
        with self._lock:
            if record.tx_id in self._records:
                raise ValueError(f"FRA journal transaction already exists: {record.tx_id}")
            self._records[record.tx_id] = record

    def get(self, tx_id: str) -> FraJournalRecord | None:
        with self._lock:
            return self._records.get(tx_id)

    def list_open(self) -> list[FraJournalRecord]:
        with self._lock:
            return [
                record
                for record in self._records.values()
                if record.state not in FINAL_JOURNAL_STATES
            ]

    def append_superseded(self, tx_id: str, item_ids: list[str]) -> None:
        with self._lock:
            record = self._records[tx_id]
            combined = tuple(dict.fromkeys((*record.superseded_ids, *item_ids)))
            self._records[tx_id] = replace(
                record,
                state=FraJournalState.SUPERSESSION_PLANNED,
                superseded_ids=combined,
            )

    def transition(
        self,
        tx_id: str,
        state: FraJournalState,
        *,
        failure_code: str | None = None,
    ) -> None:
        with self._lock:
            self._records[tx_id] = replace(
                self._records[tx_id], state=state, failure_code=failure_code
            )

    def block_buildings(self, tx_id: str, buildings: list[str]) -> None:
        with self._lock:
            for building in buildings:
                self._blocks[building.strip().lower()] = tx_id

    def unblock_buildings(self, tx_id: str, buildings: list[str]) -> None:
        with self._lock:
            for building in buildings:
                key = building.strip().lower()
                if self._blocks.get(key) == tx_id:
                    self._blocks.pop(key, None)

    def blocking_transaction(self, building: str) -> str | None:
        with self._lock:
            return self._blocks.get(building.strip().lower())


__all__ = [
    "FINAL_JOURNAL_STATES",
    "FraJournalRecord",
    "FraJournalState",
    "FraTransactionJournal",
    "InMemoryFraTransactionJournal",
    "RedisFraTransactionJournal",
    "new_fra_journal_record",
]
