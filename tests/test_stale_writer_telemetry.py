#!/usr/bin/env python3
"""VECTOR-13: stale-writer telemetry for token-guarded file transitions.

The registry's terminal/processing guard lives in an atomic Redis Lua script,
which cannot execute without a live Redis (no lupa/fakeredis here). The guard
decision is therefore mirrored by the pure ``classify_mark_state_transition``
contract function, and these tests drive the real ``RedisIngestFileRegistry.
mark_state`` through a faithful in-memory fake that reuses that function. This
exercises *every* terminal transition and asserts a stable metric is emitted on
each rejection.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

# Prime the ingest<->interfaces package import order before importing an
# interfaces submodule directly (avoids a partial-init circular import).
import ingest.context  # noqa: F401,E402  # isort: skip
from core.ingest_outcomes import TERMINAL_FILE_STATUSES
from core.telemetry import METRIC_INGEST_STALE_WRITER, get_telemetry
from interfaces.ingest_file_registry import (
    STALE_WRITER_PROCESSING_TOKEN,
    STALE_WRITER_STATE_PRECEDENCE,
    STALE_WRITER_TERMINAL_TOKEN,
    RedisIngestFileRegistry,
    classify_mark_state_transition,
)

TERMINAL = sorted(TERMINAL_FILE_STATUSES)

# For a terminal current status reached with a MATCHING transition token, the
# set of new statuses the guard accepts. Anything else is a state_precedence
# rejection. Statuses without an explicit entry carry no precedence rule and
# accept any terminal transition (token permitting).
_ACCEPTED_FROM: dict[str, set[str]] = {
    "success": {"success"},
    "critical_inconsistent": {"critical_inconsistent"},
    "failed": {"failed", "critical_inconsistent"},
    "partial": set(TERMINAL) - {"success", "degraded"},
    "degraded": set(TERMINAL) - {"success"},
}


def _accepted_new(current: str) -> set[str]:
    return _ACCEPTED_FROM.get(current, set(TERMINAL))


# ---------------------------------------------------------------------------
# Faithful in-memory fake of the mark-state Lua script
# ---------------------------------------------------------------------------


class _FakeScript:
    def __init__(self, fn):
        self._fn = fn

    def __call__(self, keys, args):
        return self._fn(keys, args)


class FakeRegistryRedis:
    """Minimal Redis stand-in whose mark-state script mirrors the Lua guard."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, str]] = {}
        self._registered = 0

    def register_script(self, _src: str):
        self._registered += 1
        # Registration order in RedisIngestFileRegistry.__init__:
        # 1) try_start, 2) record_discovered, 3) mark_state.
        if self._registered == 3:
            return _FakeScript(self._mark_state)
        return _FakeScript(lambda keys, args: 1)

    def hgetall(self, key):
        return dict(self.store.get(key, {}))

    def delete(self, key):
        self.store.pop(key, None)

    def _mark_state(self, keys, args):
        key = keys[0]
        now_epoch = int(args[0])
        now_iso = args[1]
        file_id = args[2]
        supplied_token = args[3]
        status = args[4]
        state_error = args[5]
        source_path = args[6]
        source_key = args[7]
        content_hash = args[8]
        namespaces = args[9]
        terminal = args[11] == "1"

        h = self.store.get(key, {})
        current_status = h.get("status")
        current_expiry = int(h.get("processing_expires_at_epoch") or 0)
        processing_unexpired = (
            current_status == "processing" and current_expiry > now_epoch
        )
        reason = classify_mark_state_transition(
            current_status=current_status,
            current_transition_token=h.get("transition_token", "") or "",
            current_processing_token=h.get("processing_token", "") or "",
            processing_unexpired=processing_unexpired,
            supplied_token=supplied_token,
            new_status=status,
        )
        if reason is not None:
            return [0, reason]

        def soc(supplied: str, field: str) -> str:
            return supplied if supplied != "" else h.get(field, "")

        if terminal:
            processing_token = ""
            transition_token = supplied_token
            processing_expires_at = ""
            processing_expires_at_epoch = ""
        else:
            processing_token = h.get("processing_token", "")
            transition_token = h.get("transition_token", supplied_token)
            processing_expires_at = h.get("processing_expires_at", "")
            processing_expires_at_epoch = h.get("processing_expires_at_epoch", "")

        self.store[key] = {
            "file_id": file_id,
            "source_path": soc(source_path, "source_path"),
            "source_key": soc(source_key, "source_key"),
            "content_hash": soc(content_hash, "content_hash"),
            "ingested_at_iso": now_iso,
            "namespaces": soc(namespaces, "namespaces"),
            "status": status,
            "error": state_error,
            "processing_token": processing_token,
            "transition_token": transition_token,
            "processing_expires_at": processing_expires_at,
            "processing_expires_at_epoch": processing_expires_at_epoch,
        }
        return 1


@pytest.fixture
def registry():
    get_telemetry().reset()
    return RedisIngestFileRegistry(FakeRegistryRedis())


def _seed_terminal(registry, file_id: str, *, status: str, token: str) -> None:
    registry._client.store[registry._key(file_id)] = {
        "file_id": file_id,
        "status": status,
        "transition_token": token,
        "processing_token": "",
        "processing_expires_at_epoch": "",
        "namespaces": "",
    }


def _seed_processing(registry, file_id: str, *, token: str) -> None:
    future = int(datetime.now(timezone.utc).timestamp()) + 10_000
    registry._client.store[registry._key(file_id)] = {
        "file_id": file_id,
        "status": "processing",
        "transition_token": token,
        "processing_token": token,
        "processing_expires_at_epoch": str(future),
        "namespaces": "",
    }


def _stale_count(reason: str) -> int:
    return get_telemetry().get(METRIC_INGEST_STALE_WRITER, reason=reason)


# ---------------------------------------------------------------------------
# Every terminal transition with a matching token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("current", TERMINAL)
def test_every_terminal_transition_with_matching_token(current, registry):
    """For a matching token, every current->new terminal pair either applies or
    is rejected as state_precedence, and rejections emit stable telemetry."""
    accepted = _accepted_new(current)
    for new in TERMINAL:
        get_telemetry().reset()
        file_id = f"f_{current}_{new}"
        _seed_terminal(registry, file_id, status=current, token="tok")

        if new in accepted:
            registry.mark_state(
                file_id=file_id, processing_token="tok", status=new
            )
            assert _stale_count(STALE_WRITER_STATE_PRECEDENCE) == 0
            assert registry.get(file_id).status == new
        else:
            with pytest.raises(ValueError, match="token mismatch"):
                registry.mark_state(
                    file_id=file_id, processing_token="tok", status=new
                )
            assert _stale_count(STALE_WRITER_STATE_PRECEDENCE) == 1
            # The rejected write did not change the stored status.
            assert registry.get(file_id).status == current


@pytest.mark.parametrize("current", TERMINAL)
@pytest.mark.parametrize("bad_token", ["", "wrong"])
def test_terminal_transition_with_stale_token(current, bad_token, registry):
    """A stale/absent token on any terminal record is rejected as a stale
    terminal token regardless of the attempted status (token check first)."""
    get_telemetry().reset()
    file_id = f"f_{current}_{bad_token or 'empty'}"
    _seed_terminal(registry, file_id, status=current, token="owner")

    # Even a same-status write is rejected when the token does not match.
    with pytest.raises(ValueError, match="token mismatch"):
        registry.mark_state(
            file_id=file_id, processing_token=bad_token, status=current
        )
    assert _stale_count(STALE_WRITER_TERMINAL_TOKEN) == 1
    assert _stale_count(STALE_WRITER_STATE_PRECEDENCE) == 0


def test_processing_transition_with_stale_token(registry):
    """A stale worker transitioning a file another worker is processing is
    rejected as a stale processing token."""
    get_telemetry().reset()
    _seed_processing(registry, "p1", token="owner")

    with pytest.raises(ValueError, match="token mismatch"):
        registry.mark_state(file_id="p1", processing_token="wrong", status="success")
    assert _stale_count(STALE_WRITER_PROCESSING_TOKEN) == 1


def test_processing_owner_completes_without_stale_metric(registry):
    """The owning worker (matching token) completes the file and emits no
    stale-writer telemetry."""
    get_telemetry().reset()
    _seed_processing(registry, "p2", token="owner")

    registry.mark_state(file_id="p2", processing_token="owner", status="success")
    assert registry.get("p2").status == "success"
    assert get_telemetry().snapshot().get(f"{METRIC_INGEST_STALE_WRITER}", 0) == 0
    for reason in (
        STALE_WRITER_TERMINAL_TOKEN,
        STALE_WRITER_STATE_PRECEDENCE,
        STALE_WRITER_PROCESSING_TOKEN,
    ):
        assert _stale_count(reason) == 0


def test_first_terminal_write_emits_no_stale_metric(registry):
    """Writing a terminal state for a fresh file is not a stale rejection."""
    get_telemetry().reset()
    registry.mark_state(
        file_id="fresh", processing_token="tok", status="success"
    )
    assert registry.get("fresh").status == "success"
    for reason in (
        STALE_WRITER_TERMINAL_TOKEN,
        STALE_WRITER_STATE_PRECEDENCE,
        STALE_WRITER_PROCESSING_TOKEN,
    ):
        assert _stale_count(reason) == 0


# ---------------------------------------------------------------------------
# Contract function directly
# ---------------------------------------------------------------------------


def test_classify_contract_matches_expected_matrix():
    """The pure contract function agrees with the documented accept/reject
    matrix for every matching-token terminal transition."""
    for current in TERMINAL:
        accepted = _accepted_new(current)
        for new in TERMINAL:
            reason = classify_mark_state_transition(
                current_status=current,
                current_transition_token="tok",
                current_processing_token="",
                processing_unexpired=False,
                supplied_token="tok",
                new_status=new,
            )
            if new in accepted:
                assert reason is None, (current, new)
            else:
                assert reason == STALE_WRITER_STATE_PRECEDENCE, (current, new)


def test_classify_non_terminal_states_are_unguarded():
    """discovered/absent states carry no guard; a fresh file always applies."""
    for current in (None, "discovered"):
        assert (
            classify_mark_state_transition(
                current_status=current,
                current_transition_token="",
                current_processing_token="",
                processing_unexpired=False,
                supplied_token="",
                new_status="success",
            )
            is None
        )
