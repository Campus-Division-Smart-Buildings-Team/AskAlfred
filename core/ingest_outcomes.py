"""Terminal outcome and exit-code contract for ingestion."""

from __future__ import annotations

from enum import Enum, IntEnum


class IngestTerminalStatus(str, Enum):
    """Terminal states shared by file and run outcomes."""

    SUCCESS = "success"
    SUCCESS_WITH_SKIPS = "success_with_skips"
    EMPTY_INPUT = "empty_input"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"
    NEEDS_REVIEW = "needs_review"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CRITICAL_INCONSISTENT = "critical_inconsistent"


TERMINAL_FILE_STATUSES = frozenset(status.value for status in IngestTerminalStatus)


class IngestExitCode(IntEnum):
    """Stable process exit codes documented in the failure-state plan."""

    SUCCESS = 0
    EMPTY_OR_VALIDATION = 2
    PARTIAL = 3
    UNAVAILABLE = 4
    FAILED = 5
    CRITICAL_INCONSISTENT = 10


_EXIT_CODE_BY_STATUS = {
    IngestTerminalStatus.SUCCESS: IngestExitCode.SUCCESS,
    IngestTerminalStatus.SUCCESS_WITH_SKIPS: IngestExitCode.SUCCESS,
    IngestTerminalStatus.SKIPPED: IngestExitCode.SUCCESS,
    IngestTerminalStatus.EMPTY_INPUT: IngestExitCode.EMPTY_OR_VALIDATION,
    IngestTerminalStatus.DRY_RUN: IngestExitCode.EMPTY_OR_VALIDATION,
    IngestTerminalStatus.NEEDS_REVIEW: IngestExitCode.EMPTY_OR_VALIDATION,
    IngestTerminalStatus.PARTIAL: IngestExitCode.PARTIAL,
    IngestTerminalStatus.UNAVAILABLE: IngestExitCode.UNAVAILABLE,
    IngestTerminalStatus.FAILED: IngestExitCode.FAILED,
    IngestTerminalStatus.CANCELLED: IngestExitCode.FAILED,
    IngestTerminalStatus.CRITICAL_INCONSISTENT: IngestExitCode.CRITICAL_INCONSISTENT,
}


def exit_code_for_status(status: IngestTerminalStatus | str) -> IngestExitCode:
    """Map a terminal status to its stable automation exit code."""

    stable_status = (
        status if isinstance(status, IngestTerminalStatus) else IngestTerminalStatus(status)
    )
    return _EXIT_CODE_BY_STATUS[stable_status]


__all__ = [
    "IngestExitCode",
    "IngestTerminalStatus",
    "TERMINAL_FILE_STATUSES",
    "exit_code_for_status",
]
