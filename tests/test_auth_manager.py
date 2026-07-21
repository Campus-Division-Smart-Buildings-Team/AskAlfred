#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from types import SimpleNamespace

import pytest

from auth import auth_manager
from auth.auth_manager import (
    _build_auth_context_from_claims,
    _get_or_create_auth_flow,
    _normalise_query_params,
    authentication_required,
)
from core.alfred_exceptions import ConfigError
from core.failure_codes import FailureCode
from query_core import query_context


def test_normalise_query_params_flattens_lists():
    params = {
        "code": ["abc", "latest"],
        "state": "xyz",
        "empty": [],
    }

    assert _normalise_query_params(params) == {
        "code": "latest",
        "state": "xyz",
    }


def test_authentication_is_always_required_in_production(monkeypatch):
    # authentication_required() delegates to query_context.auth_is_mandatory(),
    # the single source of truth for the mandatory-auth posture (AUTH-13).
    monkeypatch.setattr(query_context, "IS_PRODUCTION", True)
    monkeypatch.setattr(query_context, "REQUIRE_AUTH", False)
    monkeypatch.setattr(query_context, "ALLOW_ANONYMOUS_DEV", True)

    assert authentication_required() is True


def test_anonymous_access_remains_available_only_in_development(monkeypatch):
    monkeypatch.setattr(query_context, "IS_PRODUCTION", False)
    monkeypatch.setattr(query_context, "REQUIRE_AUTH", False)
    monkeypatch.setattr(query_context, "ALLOW_ANONYMOUS_DEV", True)

    assert authentication_required() is False


def test_build_auth_context_from_claims_prefers_preferred_username_and_roles():
    claims = {
        "preferred_username": "user@bristol.ac.uk",
        "name": "Test User",
        "tid": "tenant-123",
        "roles": ["viewer", "exporter"],
    }

    auth_context = _build_auth_context_from_claims(claims)

    assert auth_context.user_id == "user@bristol.ac.uk"
    assert auth_context.display_name == "Test User"
    assert auth_context.tenant_id == "tenant-123"
    assert auth_context.roles == ("viewer", "exporter")
    assert auth_context.authenticated is True
    assert auth_context.auth_source == "entra_id"


def test_build_auth_context_from_claims_falls_back_to_object_id():
    claims = {
        "oid": "object-id-123",
        "tid": "tenant-123",
    }

    auth_context = _build_auth_context_from_claims(claims)

    assert auth_context.user_id == "object-id-123"
    assert auth_context.display_name == "object-id-123"
    assert auth_context.email is None


def test_get_or_create_auth_flow_requires_client_secret(monkeypatch):
    monkeypatch.setattr(
        auth_manager,
        "st",
        SimpleNamespace(session_state={}),
    )
    monkeypatch.setattr(
        auth_manager.SecureCredentialManager,
        "get_missing_azure_credentials",
        classmethod(lambda cls, include_client_secret=True: ["AZURE_CLIENT_SECRET"]),
    )

    with pytest.raises(ConfigError, match="AZURE_CLIENT_SECRET"):
        _get_or_create_auth_flow()


def test_get_or_create_auth_flow_builds_flow_when_secret_present(monkeypatch):
    fake_streamlit = SimpleNamespace(session_state={})
    fake_flow = {
        "auth_uri": "https://login.microsoftonline.com/example/oauth2/v2.0/authorize"
    }

    class FakeMsalApp:
        def initiate_auth_code_flow(self, scopes, redirect_uri, response_mode):
            assert scopes == ["email", "User.Read"]
            assert redirect_uri == auth_manager.AUTH_REDIRECT_URI
            assert response_mode == "query"
            return fake_flow

    monkeypatch.setattr(auth_manager, "st", fake_streamlit)
    monkeypatch.setattr(
        auth_manager.SecureCredentialManager,
        "get_missing_azure_credentials",
        classmethod(lambda cls, include_client_secret=True: []),
    )
    monkeypatch.setattr(auth_manager, "build_msal_app", lambda **kwargs: FakeMsalApp())
    monkeypatch.setattr(
        auth_manager,
        "get_login_scopes",
        lambda: ["email", "User.Read"],
    )

    flow = _get_or_create_auth_flow()

    assert flow == fake_flow
    assert fake_streamlit.session_state[auth_manager.AUTH_FLOW_SESSION_KEY] == fake_flow


def test_auth_callback_error_is_not_exposed_to_ui(monkeypatch, caplog):
    provider_error = "AADSTS50011 secret-token=super-secret correlation-id=abc"
    fake_streamlit = SimpleNamespace(session_state={})

    monkeypatch.setattr(auth_manager, "st", fake_streamlit)
    monkeypatch.setattr(
        auth_manager,
        "_get_query_params",
        lambda: {"error": "invalid_request", "error_description": provider_error},
    )
    monkeypatch.setattr(auth_manager, "_clear_auth_query_params", lambda: None)

    assert auth_manager._try_complete_authentication() is None
    assert (
        fake_streamlit.session_state[auth_manager.AUTH_ERROR_SESSION_KEY]
        == auth_manager.AUTH_PROVIDER_ERROR_MESSAGE
    )
    failure = fake_streamlit.session_state[auth_manager.AUTH_FAILURE_SESSION_KEY]
    assert failure["code"] == FailureCode.AUTH_PROVIDER_RESPONSE_INVALID.value
    assert failure["component"] == auth_manager.AUTH_COMPONENT
    assert failure["correlation_id"].startswith("alf-")
    assert "auth_failure code=auth.provider_response_invalid" in caplog.text
    assert "correlation_id=alf-" in caplog.text
    assert "super-secret" not in caplog.text


def test_invalid_token_claims_are_replaced_with_safe_ui_message(monkeypatch):
    fake_streamlit = SimpleNamespace(
        session_state={
            auth_manager.AUTH_FLOW_SESSION_KEY: {
                "auth_uri": "https://example.test/sign-in"
            }
        }
    )

    class FakeMsalApp:
        def acquire_token_by_auth_code_flow(self, flow, auth_response):
            return {"id_token_claims": {"name": "No stable identifier"}}

    monkeypatch.setattr(auth_manager, "st", fake_streamlit)
    monkeypatch.setattr(
        auth_manager,
        "_get_query_params",
        lambda: {"code": "code", "state": "state"},
    )
    monkeypatch.setattr(auth_manager, "_clear_auth_query_params", lambda: None)
    monkeypatch.setattr(auth_manager, "_remove_cached_auth_flow", lambda state: None)
    monkeypatch.setattr(
        auth_manager,
        "build_msal_app",
        lambda **kwargs: FakeMsalApp(),
    )

    assert auth_manager._try_complete_authentication() is None
    assert (
        fake_streamlit.session_state[auth_manager.AUTH_ERROR_SESSION_KEY]
        == auth_manager.AUTH_INVALID_ACCOUNT_MESSAGE
    )
    failure = fake_streamlit.session_state[auth_manager.AUTH_FAILURE_SESSION_KEY]
    assert failure["code"] == FailureCode.AUTH_CLAIMS_INVALID.value
    assert failure["retryable"] is False
    assert failure["correlation_id"].startswith("alf-")


def test_token_exchange_exception_has_structured_retryable_failure(
    monkeypatch, caplog
):
    fake_streamlit = SimpleNamespace(
        session_state={
            auth_manager.AUTH_FLOW_SESSION_KEY: {
                "auth_uri": "https://example.test/sign-in"
            }
        }
    )

    class FakeMsalApp:
        def acquire_token_by_auth_code_flow(self, flow, auth_response):
            raise RuntimeError("secret-token=super-secret-value-123456789")

    monkeypatch.setattr(auth_manager, "st", fake_streamlit)
    monkeypatch.setattr(
        auth_manager,
        "_get_query_params",
        lambda: {"code": "code", "state": "state"},
    )
    monkeypatch.setattr(auth_manager, "_clear_auth_query_params", lambda: None)
    monkeypatch.setattr(auth_manager, "build_msal_app", lambda **kwargs: FakeMsalApp())

    assert auth_manager._try_complete_authentication() is None

    failure = fake_streamlit.session_state[auth_manager.AUTH_FAILURE_SESSION_KEY]
    assert failure["code"] == FailureCode.AUTH_PROVIDER_UNAVAILABLE.value
    assert failure["retryable"] is True
    assert failure["correlation_id"].startswith("alf-")
    assert "auth_failure code=auth.provider_unavailable" in caplog.text
    assert "correlation_id=alf-" in caplog.text
    assert "super-secret-value-123456789" not in caplog.text
