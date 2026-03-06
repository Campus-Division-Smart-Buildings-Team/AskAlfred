#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Microsoft Authentication Library (MSAL) configuration for Azure AD.
Handles secure credential loading from credential manager.
"""

import logging
from typing import Optional

import msal

from alfred_exceptions import ConfigError
from credential_manager import SecureCredentialManager
from log_sanitiser import sanitise_error

logger = logging.getLogger(__name__)


def _get_azure_config() -> dict[str, str]:
    """
    Load Azure AD configuration from credential manager.

    Does NOT store credentials in memory - retrieves fresh from environment each time.
    """
    try:
        return SecureCredentialManager.get_azure_config()
    except KeyError as e:
        raise ConfigError(
            "Azure credentials not configured. "
            "Please set AZURE_TENANT_ID, AZURE_CLIENT_ID, and AZURE_CLIENT_SECRET "
            "environment variables."
        ) from e


def _get_authority() -> str:
    """
    Build MSAL authority URL.

    Uses credential manager to get tenant ID securely.
    """
    try:
        tenant_id = SecureCredentialManager.get_azure_tenant_id()
        return f"https://login.microsoftonline.com/{tenant_id}"
    except KeyError:
        # Return generic authority if tenant not available yet
        return "https://login.microsoftonline.com/common"


SCOPES = ["User.Read"]  # minimal, safe default


def build_msal_app(
    cache: Optional[object] = None,
) -> msal.ConfidentialClientApplication:
    """
    Build MSAL ConfidentialClientApplication for Azure AD authentication.

    Credentials are loaded on-demand from environment, not cached in memory.

    Args:
        cache: Optional token cache for credential storage

    Returns:
        Configured MSAL ConfidentialClientApplication instance

    Raises:
        ConfigError: If Azure credentials are not available
    """
    try:
        config = _get_azure_config()

        return msal.ConfidentialClientApplication(
            client_id=config.get("AZURE_CLIENT_ID"),
            authority=_get_authority(),
            client_credential=config.get("AZURE_CLIENT_SECRET"),
            token_cache=cache,
        )
    except ConfigError:
        raise
    except Exception as e:
        logger.error("Failed to build MSAL application: %s", sanitise_error(e))
        raise ConfigError("Failed to initialize Azure AD authentication") from e
