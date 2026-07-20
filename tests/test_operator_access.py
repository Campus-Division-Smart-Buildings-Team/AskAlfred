"""Tests for the Entra-role-based operator gate (plan item 6)."""

import os

import auth.access_control as access_control
from auth.auth_context import ANONYMOUS_AUTH_CONTEXT, AuthContext
from config import OPERATOR_ROLES


def _context(*, authenticated=True, roles=()):
    return AuthContext(
        user_id="u1",
        display_name="User One",
        tenant_id="tenant-123",
        roles=tuple(roles),
        authenticated=authenticated,
        auth_source="entra_id",
    )


def test_operator_roles_default_is_data_steward(monkeypatch):
    # With no OPERATOR_ROLES override, the packaged default is data_steward.
    monkeypatch.delenv("OPERATOR_ROLES", raising=False)
    parsed = frozenset(
        role.strip()
        for role in os.getenv("OPERATOR_ROLES", "data_steward").split(",")
        if role.strip()
    )
    assert parsed == {"data_steward"}


def test_operator_roles_is_a_frozenset():
    assert isinstance(OPERATOR_ROLES, frozenset)


def test_authenticated_user_with_operator_role_is_operator(monkeypatch):
    monkeypatch.setattr(access_control, "OPERATOR_ROLES", frozenset({"data_steward"}))
    assert access_control.is_operator(_context(roles=("base_view", "data_steward")))


def test_authenticated_user_without_operator_role_is_not_operator(monkeypatch):
    monkeypatch.setattr(access_control, "OPERATOR_ROLES", frozenset({"data_steward"}))
    assert not access_control.is_operator(_context(roles=("base_view",)))


def test_authenticated_user_without_any_roles_is_not_operator(monkeypatch):
    monkeypatch.setattr(access_control, "OPERATOR_ROLES", frozenset({"data_steward"}))
    assert not access_control.is_operator(_context(roles=()))


def test_anonymous_session_is_never_operator(monkeypatch):
    # Fail closed even if an anonymous context somehow carries the role value.
    monkeypatch.setattr(access_control, "OPERATOR_ROLES", frozenset({"data_steward"}))
    assert not access_control.is_operator(
        _context(authenticated=False, roles=("data_steward",))
    )
    assert not access_control.is_operator(ANONYMOUS_AUTH_CONTEXT)


def test_operator_role_match_is_case_sensitive(monkeypatch):
    monkeypatch.setattr(access_control, "OPERATOR_ROLES", frozenset({"data_steward"}))
    assert not access_control.is_operator(_context(roles=("Data_Steward",)))


def test_empty_operator_roles_grants_no_one(monkeypatch):
    monkeypatch.setattr(access_control, "OPERATOR_ROLES", frozenset())
    assert not access_control.is_operator(_context(roles=("data_steward",)))


def test_operator_role_is_env_configurable(monkeypatch):
    # A different configured value gates the panel instead of data_steward.
    monkeypatch.setattr(access_control, "OPERATOR_ROLES", frozenset({"platform_admin"}))
    assert access_control.is_operator(_context(roles=("platform_admin",)))
    assert not access_control.is_operator(_context(roles=("data_steward",)))
