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
    DEGRADED = "degraded"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CRITICAL_INCONSISTENT = "critical_inconsistent"


TERMINAL_FILE_STATUSES = frozenset(status.value for status in IngestTerminalStatus)


# INGEST-08: stable, low-cardinality reasons recorded on the ``error`` field of
# a ``needs_review`` file so an operator can tell *why* a document produced no
# usable vectors. These are review outcomes, kept deliberately distinct from a
# technical extraction/embedding ``failed``:
#   - empty_document     -> nothing could be extracted from the file at all
#   - unsupported_layout -> content was extracted but yielded no usable vectors
#   - fra_no_action_plan -> an FRA had no locatable action-plan section
REVIEW_EMPTY_DOCUMENT = "empty_document"
REVIEW_UNSUPPORTED_LAYOUT = "unsupported_layout"
REVIEW_FRA_NO_ACTION_PLAN = "fra_no_action_plan"

NEEDS_REVIEW_REASONS = frozenset(
    {
        REVIEW_EMPTY_DOCUMENT,
        REVIEW_UNSUPPORTED_LAYOUT,
        REVIEW_FRA_NO_ACTION_PLAN,
    }
)


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
    # A degraded file/run committed all of its vectors through a
    # reduced-fidelity fallback (e.g. lossy text decoding). It completed, so it
    # maps to the success exit code, but the terminal status still records that
    # full fidelity was not claimed.
    IngestTerminalStatus.DEGRADED: IngestExitCode.SUCCESS,
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
    "NEEDS_REVIEW_REASONS",
    "REVIEW_EMPTY_DOCUMENT",
    "REVIEW_UNSUPPORTED_LAYOUT",
    "REVIEW_FRA_NO_ACTION_PLAN",
    "exit_code_for_status",
]
