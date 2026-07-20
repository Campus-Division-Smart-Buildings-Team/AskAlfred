# IngestFileRegistry port
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol, cast

from redis import Redis

from config.constant import (
    INGEST_FILE_TTL_FAILED_SECONDS,
    INGEST_FILE_TTL_PROCESSING_SECONDS,
    INGEST_FILE_TTL_SUCCESS_SECONDS,
)


@dataclass(frozen=True)
class FileRecord:
    file_id: str
    source_path: str
    source_key: str
    content_hash: str | None
    ingested_at_iso: str
    namespaces: tuple[str, ...]
    status: str  # "success" | "failed" | "partial"
    error: str | None = None
    processing_token: str | None = None
    processing_expires_at: str | None = None
    processing_expires_at_epoch: int | None = None


class IngestFileRegistry(Protocol):
    def get(self, file_id: str) -> Optional[FileRecord]: ...
    def has_record(self, file_id: str) -> bool: ...
    def is_success(self, file_id: str) -> bool: ...
    def bulk_exists(self, file_ids: Iterable[str]) -> dict[str, bool]: ...
    def upsert(self, record: FileRecord) -> None: ...

    def upsert_with_token(
        self, record: FileRecord, *, processing_token: str | None
    ) -> None: ...

    def record_discovered(
        self,
        *,
        file_id: str,
        source_path: str,
        source_key: str,
        content_hash: str | None,
    ) -> None: ...

    def try_start_processing(
        self,
        *,
        file_id: str,
        lease_seconds: int,
        processing_token: str,
        source_path: str,
        source_key: str,
        content_hash: str | None,
    ) -> bool: ...

    def mark_state(
        self,
        *,
        file_id: str,
        processing_token: str | None,
        status: str,
        error: str | None = None,
        source_path: str | None = None,
        source_key: str | None = None,
        content_hash: str | None = None,
        namespaces: tuple[str, ...] | None = None,
    ) -> None: ...
    def delete(self, file_id: str) -> None: ...


class RedisIngestFileRegistry:
    """Redis-backed IngestFileRegistry using JSON-encoded records."""

    def __init__(
        self,
        client: Redis,
        *,
        prefix: str = "ingest:file:",
        success_ttl_seconds: int = INGEST_FILE_TTL_SUCCESS_SECONDS,
        failed_ttl_seconds: int = INGEST_FILE_TTL_FAILED_SECONDS,
        processing_ttl_seconds: int = INGEST_FILE_TTL_PROCESSING_SECONDS,
    ):
        self._client = client
        self._prefix = prefix
        self._success_ttl_seconds = int(success_ttl_seconds)
        self._failed_ttl_seconds = int(failed_ttl_seconds)
        self._processing_ttl_seconds = int(processing_ttl_seconds)
        self._try_start_script = self._client.register_script("""
            local key = KEYS[1]
            local now_epoch = tonumber(ARGV[1])
            local status = ARGV[2]
            local token = ARGV[3]
            local expires_epoch = tonumber(ARGV[4])
            local expires_iso = ARGV[5]
            local ingested_at = ARGV[6]
            local source_path = ARGV[7]
            local source_key = ARGV[8]
            local content_hash = ARGV[9]
            local namespaces_json = ARGV[10]
            local file_id = ARGV[11]
            local ttl_seconds = tonumber(ARGV[12])

            local current_status = redis.call("HGET", key, "status")
            if current_status then
                local current_expiry = tonumber(redis.call("HGET", key, "processing_expires_at_epoch") or "0")
                if current_status == "processing" and current_expiry > now_epoch then
                    return 0
                end
                if current_status == "success" then
                    return 0
                end
            end

            redis.call(
                "HSET",
                key,
                "file_id", file_id,
                "source_path", source_path,
                "source_key", source_key,
                "content_hash", content_hash,
                "ingested_at_iso", ingested_at,
                "namespaces", namespaces_json,
                "status", status,
                "error", "",
                "processing_token", token,
                "processing_expires_at", expires_iso,
                "processing_expires_at_epoch", tostring(expires_epoch)
            )
            redis.call("EXPIRE", key, ttl_seconds)
            return 1
            """)
        self._record_discovered_script = self._client.register_script("""
            local key = KEYS[1]
            local now_iso = ARGV[1]
            local source_path = ARGV[2]
            local source_key = ARGV[3]
            local content_hash = ARGV[4]
            local namespaces_json = ARGV[5]
            local file_id = ARGV[6]
            local ttl_seconds = tonumber(ARGV[7])

            local exists = redis.call("EXISTS", key)
            if exists == 1 then
                return 0
            end

            redis.call(
                "HSET",
                key,
                "file_id", file_id,
                "source_path", source_path,
                "source_key", source_key,
                "content_hash", content_hash,
                "ingested_at_iso", now_iso,
                "namespaces", namespaces_json,
                "status", "discovered",
                "error", "",
                "processing_token", "",
                "processing_expires_at", "",
                "processing_expires_at_epoch", ""
            )
            redis.call("EXPIRE", key, ttl_seconds)
            return 1
            """)
        self._mark_state_script = self._client.register_script("""
            local key = KEYS[1]
            local now_epoch = tonumber(ARGV[1])
            local now_iso = ARGV[2]
            local file_id = ARGV[3]
            local supplied_token = ARGV[4]
            local status = ARGV[5]
            local state_error = ARGV[6]
            local source_path = ARGV[7]
            local source_key = ARGV[8]
            local content_hash = ARGV[9]
            local namespaces = ARGV[10]
            local ttl_seconds = tonumber(ARGV[11])
            local terminal = ARGV[12] == "1"

            local current_status = redis.call("HGET", key, "status")
            if current_status == "processing" then
                local current_expiry = tonumber(
                    redis.call("HGET", key, "processing_expires_at_epoch") or "0"
                )
                if current_expiry > now_epoch then
                    local current_token = redis.call("HGET", key, "processing_token") or ""
                    if supplied_token == "" or supplied_token ~= current_token then
                        return 0
                    end
                end
            end

            local function supplied_or_current(supplied, field)
                if supplied ~= "" then
                    return supplied
                end
                return redis.call("HGET", key, field) or ""
            end

            local processing_token = ""
            local processing_expires_at = ""
            local processing_expires_at_epoch = ""
            if not terminal then
                processing_token = redis.call("HGET", key, "processing_token") or ""
                processing_expires_at = redis.call(
                    "HGET", key, "processing_expires_at"
                ) or ""
                processing_expires_at_epoch = redis.call(
                    "HGET", key, "processing_expires_at_epoch"
                ) or ""
            end

            redis.call(
                "HSET",
                key,
                "file_id", file_id,
                "source_path", supplied_or_current(source_path, "source_path"),
                "source_key", supplied_or_current(source_key, "source_key"),
                "content_hash", supplied_or_current(content_hash, "content_hash"),
                "ingested_at_iso", now_iso,
                "namespaces", supplied_or_current(namespaces, "namespaces"),
                "status", status,
                "error", state_error,
                "processing_token", processing_token,
                "processing_expires_at", processing_expires_at,
                "processing_expires_at_epoch", processing_expires_at_epoch
            )
            redis.call("EXPIRE", key, ttl_seconds)
            return 1
            """)

    def _key(self, file_id: str) -> str:
        return f"{self._prefix}{file_id}"

    def _ttl_for_status(self, status: str, *, lease_seconds: int | None = None) -> int:
        normalized = (status or "").lower()
        if normalized == "success":
            return self._success_ttl_seconds
        if normalized in ("failed", "partial"):
            return self._failed_ttl_seconds
        if normalized in ("processing", "discovered"):
            if lease_seconds is None:
                return self._processing_ttl_seconds
            return max(self._processing_ttl_seconds, int(lease_seconds))
        return self._failed_ttl_seconds

    @staticmethod
    def _decode_value(raw: object) -> Optional[str]:
        if isinstance(raw, (bytes, bytearray)):
            return raw.decode("utf-8")
        if isinstance(raw, str):
            return raw
        return None

    def get(self, file_id: str) -> Optional[FileRecord]:
        payload = cast(dict[object, object], self._client.hgetall(self._key(file_id)))
        if not payload:
            return None
        decoded: dict[str, str] = {}
        for key, value in payload.items():
            raw_key = self._decode_value(key)
            raw_value = self._decode_value(value)
            if raw_key is None or raw_value is None:
                continue
            decoded[raw_key] = raw_value
        namespaces_raw = decoded.get("namespaces") or "[]"
        try:
            namespaces = tuple(json.loads(namespaces_raw) or ())
        except (TypeError, json.JSONDecodeError):
            namespaces = ()
        content_hash = decoded.get("content_hash") or None
        error = decoded.get("error") or None
        processing_token = decoded.get("processing_token") or None
        processing_expires_at = decoded.get("processing_expires_at") or None
        processing_expires_at_epoch = decoded.get("processing_expires_at_epoch")
        try:
            processing_expires_at_epoch_int = (
                int(processing_expires_at_epoch)
                if processing_expires_at_epoch
                else None
            )
        except (TypeError, ValueError):
            processing_expires_at_epoch_int = None
        return FileRecord(
            file_id=decoded.get("file_id", file_id),
            source_path=decoded.get("source_path", ""),
            source_key=decoded.get("source_key", ""),
            content_hash=content_hash,
            ingested_at_iso=decoded.get("ingested_at_iso", ""),
            namespaces=namespaces,
            status=decoded.get("status", "unknown"),
            error=error,
            processing_token=processing_token,
            processing_expires_at=processing_expires_at,
            processing_expires_at_epoch=processing_expires_at_epoch_int,
        )

    def has_record(self, file_id: str) -> bool:
        record = self.get(file_id)
        if not record:
            return False
        return record.status in (
            "discovered",
            "processing",
            "failed",
            "partial",
            "success",
        )

    def is_success(self, file_id: str) -> bool:
        record = self.get(file_id)
        if not record:
            return False
        return record.status == "success"

    def bulk_exists(self, file_ids: Iterable[str]) -> dict[str, bool]:
        ids = list(file_ids)
        if not ids:
            return {}
        pipeline = self._client.pipeline()
        for file_id in ids:
            pipeline.exists(self._key(file_id))
        results = cast(list[object], pipeline.execute() or [])
        return {file_id: bool(results[i]) for i, file_id in enumerate(ids)}

    def upsert(self, record: FileRecord) -> None:
        payload = {
            "file_id": record.file_id,
            "source_path": record.source_path,
            "source_key": record.source_key,
            "content_hash": record.content_hash or "",
            "ingested_at_iso": record.ingested_at_iso,
            "namespaces": json.dumps(list(record.namespaces), ensure_ascii=False),
            "status": record.status,
            "error": record.error or "",
            "processing_token": record.processing_token or "",
            "processing_expires_at": record.processing_expires_at or "",
            "processing_expires_at_epoch": str(
                record.processing_expires_at_epoch or ""
            ),
        }
        ttl_seconds = self._ttl_for_status(record.status)
        pipeline = self._client.pipeline(transaction=False)
        pipeline.hset(self._key(record.file_id), mapping=payload)
        pipeline.expire(self._key(record.file_id), ttl_seconds)
        pipeline.execute()

    def upsert_with_token(
        self,
        record: FileRecord,
        *,
        processing_token: str | None,
    ) -> None:
        self.mark_state(
            file_id=record.file_id,
            processing_token=processing_token,
            status=record.status,
            error=record.error,
            source_path=record.source_path,
            source_key=record.source_key,
            content_hash=record.content_hash,
            namespaces=record.namespaces,
        )

    def record_discovered(
        self,
        *,
        file_id: str,
        source_path: str,
        source_key: str,
        content_hash: str | None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat() + "Z"
        self._record_discovered_script(
            keys=[self._key(file_id)],
            args=[
                now_iso,
                source_path,
                source_key,
                content_hash or "",
                json.dumps([], ensure_ascii=False),
                file_id,
                str(self._ttl_for_status("discovered")),
            ],
        )

    def try_start_processing(
        self,
        *,
        file_id: str,
        lease_seconds: int,
        processing_token: str,
        source_path: str,
        source_key: str,
        content_hash: str | None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=lease_seconds)
        result = self._try_start_script(
            keys=[self._key(file_id)],
            args=[
                str(int(now.timestamp())),
                "processing",
                processing_token,
                str(int(expires_at.timestamp())),
                expires_at.isoformat() + "Z",
                now.isoformat() + "Z",
                source_path,
                source_key,
                content_hash or "",
                json.dumps([], ensure_ascii=False),
                file_id,
                str(self._ttl_for_status("processing", lease_seconds=lease_seconds)),
            ],
        )
        return bool(result)

    def mark_state(
        self,
        *,
        file_id: str,
        processing_token: str | None,
        status: str,
        error: str | None = None,
        source_path: str | None = None,
        source_key: str | None = None,
        content_hash: str | None = None,
        namespaces: tuple[str, ...] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        terminal = status in ("success", "failed")
        updated = self._mark_state_script(
            keys=[self._key(file_id)],
            args=[
                str(int(now.timestamp())),
                now.isoformat() + "Z",
                file_id,
                processing_token or "",
                status,
                error or "",
                source_path or "",
                source_key or "",
                content_hash or "",
                (
                    json.dumps(list(namespaces), ensure_ascii=False)
                    if namespaces
                    else ""
                ),
                str(self._ttl_for_status(status)),
                "1" if terminal else "0",
            ],
        )
        if not updated:
            raise ValueError(f"Rejecting state update for {file_id}: token mismatch")

    def delete(self, file_id: str) -> None:
        self._client.delete(self._key(file_id))


class NoOpIngestFileRegistry:
    """No-op IngestFileRegistry for dry-run mode."""

    def get(self, file_id: str) -> Optional[FileRecord]:
        # pylint: disable=unused-argument
        return None

    def has_record(self, file_id: str) -> bool:
        # pylint: disable=unused-argument
        return False

    def is_success(self, file_id: str) -> bool:
        # pylint: disable=unused-argument
        return False

    def bulk_exists(self, file_ids: Iterable[str]) -> dict[str, bool]:
        # pylint: disable=unused-argument
        return {file_id: False for file_id in file_ids}

    def upsert(self, record: FileRecord) -> None:
        # pylint: disable=unused-argument
        return None

    def upsert_with_token(
        self,
        record: FileRecord,
        *,
        processing_token: str | None,
    ) -> None:
        # pylint: disable=unused-argument
        return None

    def record_discovered(
        self,
        *,
        file_id: str,
        source_path: str,
        source_key: str,
        content_hash: str | None,
    ) -> None:
        # pylint: disable=unused-argument
        return None

    def try_start_processing(
        self,
        *,
        file_id: str,
        lease_seconds: int,
        processing_token: str,
        source_path: str,
        source_key: str,
        content_hash: str | None,
    ) -> bool:
        # pylint: disable=unused-argument
        return True

    def mark_state(
        self,
        *,
        file_id: str,
        processing_token: str | None,
        status: str,
        error: str | None = None,
        source_path: str | None = None,
        source_key: str | None = None,
        content_hash: str | None = None,
        namespaces: tuple[str, ...] | None = None,
    ) -> None:
        # pylint: disable=unused-argument
        return None

    def delete(self, file_id: str) -> None:
        # pylint: disable=unused-argument
        return None


__all__ = [
    "FileRecord",
    "IngestFileRegistry",
    "RedisIngestFileRegistry",
    "NoOpIngestFileRegistry",
]
