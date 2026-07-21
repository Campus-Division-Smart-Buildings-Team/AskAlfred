#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Central presenter that maps structured outcomes to user-safe copy.

This is the single place that turns an :class:`OutcomeStatus` (and any attached
:class:`FailureInfo`) into what a user sees. It never renders exception text,
credentials, endpoints, index/namespace names, environment-variable names,
provider payloads, or security-rule detail. The only machine detail it exposes
is the opaque correlation reference (``alf-xxxxxxxxxxxx``) so support can locate
the sanitised server-side log for an incident.

Keep the mapping functions free of Streamlit so they can be unit-tested without
a running app; only :func:`render_query_failure` touches ``st``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.failure_codes import FailureCode
from core.outcomes import FailureInfo, OutcomeStatus, new_correlation_id

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from query_core.query_result import QueryResult


@dataclass(frozen=True)
class PresentedOutcome:
    """User-safe presentation of a single operation outcome.

    Attributes:
        severity: Streamlit-style notice level (``success``/``info``/
            ``warning``/``error``). ``success`` means no notice is needed.
        message: Plain-language impact statement. Static copy only.
        action: One primary next action for the user (may be empty).
        retry_suggested: Whether retrying is likely to help.
        reference: Opaque correlation reference for support, or ``None``.
        render_as_notice: Whether this outcome needs a dedicated notice
            component rather than being shown as an ordinary answer.
    """

    severity: str
    message: str
    action: str
    retry_suggested: bool
    reference: str | None
    render_as_notice: bool


# Static, user-safe copy for each terminal status. Nothing here is interpolated
# from a failure, exception, or request, so these strings cannot leak detail.
_STATUS_PRESENTATION: dict[OutcomeStatus, dict[str, object]] = {
    OutcomeStatus.SUCCESS: {
        "severity": "success",
        "message": "",
        "action": "",
        "retry_suggested": False,
        "render_as_notice": False,
    },
    OutcomeStatus.EMPTY: {
        "severity": "info",
        "message": "I couldn't find matching information for that.",
        "action": "Try adding a building name, document type, or date.",
        "retry_suggested": False,
        "render_as_notice": True,
    },
    OutcomeStatus.LOW_CONFIDENCE: {
        "severity": "warning",
        "message": (
            "I found some possible matches, but I'm not confident they answer "
            "your question."
        ),
        "action": "Try rephrasing with more specific detail.",
        "retry_suggested": False,
        "render_as_notice": True,
    },
    OutcomeStatus.REJECTED: {
        "severity": "warning",
        "message": "I couldn't process that request.",
        "action": "Please adjust your question and try again.",
        "retry_suggested": False,
        "render_as_notice": True,
    },
    OutcomeStatus.DEGRADED: {
        "severity": "warning",
        "message": "Some information may be missing from this answer.",
        "action": "",
        "retry_suggested": False,
        "render_as_notice": True,
    },
    OutcomeStatus.PARTIAL: {
        "severity": "warning",
        "message": (
            "I could only reach some of the information, so these results may "
            "be incomplete."
        ),
        "action": "Try again shortly for complete results.",
        "retry_suggested": True,
        "render_as_notice": True,
    },
    OutcomeStatus.UNAVAILABLE: {
        "severity": "error",
        "message": "I can't complete that request right now.",
        "action": "Please try again in a few minutes.",
        "retry_suggested": True,
        "render_as_notice": True,
    },
    OutcomeStatus.FAILED: {
        "severity": "error",
        "message": "Something went wrong while answering that question.",
        "action": "Please try again.",
        "retry_suggested": True,
        "render_as_notice": True,
    },
    OutcomeStatus.CRITICAL_INCONSISTENT: {
        "severity": "error",
        "message": "I can't complete that request right now.",
        "action": "Please contact support if this continues.",
        "retry_suggested": False,
        "render_as_notice": True,
    },
}


# Failure-code-specific overrides. These are privacy-preserving: an access
# rejection must not reveal whether inaccessible documents exist, so it reads as
# an account-provisioning problem rather than a search result.
_FAILURE_CODE_OVERRIDES: dict[FailureCode, dict[str, object]] = {
    FailureCode.INPUT_BUILDING_SCOPE_INVALID: {
        "severity": "warning",
        "message": "I couldn't identify the building in that request.",
        "action": "Please provide the building name and try again.",
        "retry_suggested": False,
    },
    FailureCode.ACCESS_CONTEXT_INVALID: {
        "severity": "error",
        "message": "Your account could not be assigned data access.",
        "action": "Please contact support so your access can be set up.",
        "retry_suggested": False,
    },
    FailureCode.ACCESS_ROLE_CONTEXT_INVALID: {
        "severity": "error",
        "message": "Your account could not be assigned data access.",
        "action": "Please contact support so your access can be set up.",
        "retry_suggested": False,
    },
}


# Statuses for which a support reference should always be present, even when no
# structured failure is attached (e.g. a legacy ``success=False`` result).
_ALWAYS_REFERENCE_STATUSES = frozenset(
    {
        OutcomeStatus.UNAVAILABLE,
        OutcomeStatus.FAILED,
        OutcomeStatus.CRITICAL_INCONSISTENT,
    }
)


def present_outcome(
    status: OutcomeStatus | str,
    failure: FailureInfo | None = None,
) -> PresentedOutcome:
    """Map a status (and optional failure) to user-safe presentation copy."""

    resolved_status = (
        status if isinstance(status, OutcomeStatus) else OutcomeStatus(status)
    )
    base = dict(_STATUS_PRESENTATION[resolved_status])

    if failure is not None:
        override = _FAILURE_CODE_OVERRIDES.get(failure.code)
        if override:
            base.update(override)

    reference: str | None = None
    if failure is not None:
        reference = failure.correlation_id
    elif resolved_status in _ALWAYS_REFERENCE_STATUSES:
        # Guarantee a support handle even when a failure object was not built.
        reference = new_correlation_id()

    return PresentedOutcome(
        severity=str(base["severity"]),
        message=str(base["message"]),
        action=str(base["action"]),
        retry_suggested=bool(base["retry_suggested"]),
        reference=reference,
        render_as_notice=bool(base["render_as_notice"]),
    )


def present_query_failure(result: "QueryResult") -> PresentedOutcome:
    """Presentation for a completed query result's structured outcome."""

    return present_outcome(result.status, result.failure)


# ---------------------------------------------------------------------------
# Presenter kill-switch (Phase 5)
# ---------------------------------------------------------------------------
# The presenter is a hard dependency of every failure surface, so a bug here or
# a deliberate rollback must degrade to a single, static, user-safe notice
# rather than raising a fresh unhandled exception. This copy interpolates
# nothing, so it is safe even when the structured presenter is disabled.
_FALLBACK_MESSAGE = "I can't complete that request right now."
_FALLBACK_ACTION = "Please try again in a few minutes."


def _fallback_presented() -> PresentedOutcome:
    """Return the static, always-safe outcome used when presentation fails."""

    return PresentedOutcome(
        severity="error",
        message=_FALLBACK_MESSAGE,
        action=_FALLBACK_ACTION,
        retry_suggested=True,
        reference=new_correlation_id(),
        render_as_notice=True,
    )


def safe_present_outcome(
    status: OutcomeStatus | str,
    failure: FailureInfo | None = None,
) -> PresentedOutcome:
    """Presenter entry point that never raises and honours the kill-switch.

    When ``USE_STRUCTURED_PRESENTER`` is cleared, or when the structured
    presenter raises for any reason, this returns the static fallback outcome so
    a presentation fault cannot escape to Streamlit as an unhandled exception.
    """

    from config import feature_flags

    if not feature_flags.use_structured_presenter():
        return _fallback_presented()
    try:
        return present_outcome(status, failure)
    except Exception:  # pylint: disable=broad-except
        logger.exception("error_presenter.present_outcome failed; using fallback")
        return _fallback_presented()


def safe_present_query_failure(result: "QueryResult") -> PresentedOutcome:
    """Kill-switch-protected presentation for a completed query result."""

    try:
        return safe_present_outcome(result.status, result.failure)
    except Exception:  # pylint: disable=broad-except
        logger.exception("error_presenter.present_query_failure failed; fallback")
        return _fallback_presented()


def render_query_failure(presented: PresentedOutcome) -> str:
    """Render a presented outcome as a Streamlit notice and return its text.

    The returned string is what should be stored in chat history so the
    transcript matches what the user saw. Streamlit is imported lazily so the
    mapping functions above stay usable without a running app. Rendering is
    wrapped so that a Streamlit failure downgrades to a plain error notice
    instead of escaping as an unhandled exception (presenter kill-switch).
    """

    import streamlit as st

    try:
        caption_parts: list[str] = []
        if presented.action:
            caption_parts.append(presented.action)
        if presented.reference:
            caption_parts.append(f"Reference: {presented.reference}")

        notice = {
            "success": st.success,
            "info": st.info,
            "warning": st.warning,
            "error": st.error,
        }.get(presented.severity, st.error)

        notice(presented.message)
        if caption_parts:
            st.caption("  •  ".join(caption_parts))

        return presented.message
    except Exception:  # pylint: disable=broad-except
        logger.exception("render_query_failure failed; rendering static fallback")
        try:
            st.error(_FALLBACK_MESSAGE)
        except Exception:  # pylint: disable=broad-except
            logger.exception("static fallback notice also failed")
        return _FALLBACK_MESSAGE


__all__ = [
    "PresentedOutcome",
    "present_outcome",
    "present_query_failure",
    "render_query_failure",
    "safe_present_outcome",
    "safe_present_query_failure",
]
