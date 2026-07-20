"""Tests for pre-retrieval access-context validation (AUTH-08)."""

import logging

from core.failure_codes import FailureCode
from core.outcomes import OutcomeStatus
from query_core.query_context import (
    ACCESS_CONTROL_COMPONENT,
    validate_access_context,
)
from query_core.query_manager import QueryManager


def test_authenticated_without_tenant_is_rejected_before_retrieval():
    failure = validate_access_context(
        authenticated=True,
        tenant_id=None,
        user_roles=(),
    )

    assert failure is not None
    assert failure.code is FailureCode.ACCESS_CONTEXT_INVALID
    assert failure.component == ACCESS_CONTROL_COMPONENT
    # An access-context violation is a configuration problem, not transient.
    assert failure.retryable is False
    assert failure.correlation_id


def test_authenticated_with_tenant_proceeds():
    assert (
        validate_access_context(
            authenticated=True,
            tenant_id="tenant-123",
            user_roles=("reader",),
        )
        is None
    )


def test_authenticated_without_roles_is_rejected_before_retrieval():
    failure = validate_access_context(
        authenticated=True,
        tenant_id="tenant-123",
        user_roles=(),
    )

    assert failure is not None
    assert failure.code is FailureCode.ACCESS_ROLE_CONTEXT_INVALID
    assert failure.component == ACCESS_CONTROL_COMPONENT
    assert failure.retryable is False
    assert failure.correlation_id


def test_authenticated_with_only_blank_roles_is_rejected():
    failure = validate_access_context(
        authenticated=True,
        tenant_id="tenant-123",
        user_roles=("", "  "),
    )

    assert failure is not None
    assert failure.code is FailureCode.ACCESS_ROLE_CONTEXT_INVALID


def test_anonymous_session_is_not_rejected_here():
    # The anonymous/unfiltered posture (AUTH-13) is decided separately.
    assert (
        validate_access_context(
            authenticated=False,
            tenant_id=None,
            user_roles=(),
        )
        is None
    )


def test_empty_string_tenant_is_treated_as_missing():
    failure = validate_access_context(
        authenticated=True,
        tenant_id="",
        user_roles=(),
    )

    assert failure is not None
    assert failure.code is FailureCode.ACCESS_CONTEXT_INVALID


def test_query_manager_rejects_roleless_context_before_any_handler_runs(caplog):
    manager = object.__new__(QueryManager)
    manager.logger = logging.getLogger("test.access_context")
    caplog.set_level(logging.WARNING, logger="test.access_context")

    result = manager.process_query(
        "Show the latest FRA",
        authenticated=True,
        tenant_id="tenant-123",
        user_roles=(),
    )

    assert result.status is OutcomeStatus.REJECTED
    assert result.failure is not None
    assert result.failure.code is FailureCode.ACCESS_ROLE_CONTEXT_INVALID
    assert result.handler_used == ACCESS_CONTROL_COMPONENT
    assert "code=access.role_context_invalid" in caplog.text
    assert f"correlation_id={result.failure.correlation_id}" in caplog.text
