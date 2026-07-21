#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime feature flags for the failure-handling rollout (Phase 5).

Phase 5 rolls the structured outcome contract and the central error presenter
out behind flags so that a regression can be disabled without a redeploy and so
that a fault in error presentation can be contained (the delivery-risk
"presenter kill-switch").

Every flag is read from the environment on each call rather than captured at
import time, so an operator can flip a value between requests. Flags fail
*safe*: the default keeps the new, tested behaviour on, and the only thing an
operator can do by clearing a flag is fall back to the previous, simpler path.

Never route security decisions through these flags; they only select between two
already-safe presentation/orchestration paths.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    """Return a boolean environment flag, tolerating unset/blank values."""

    raw = os.getenv(name)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in _TRUTHY:
        return True
    if token in _FALSY:
        return False
    return default


def is_production() -> bool:
    """Return whether the app is running in a production deployment.

    Read live from the environment so callers (e.g. the fault-injection gate)
    cannot be fooled by an import-time snapshot in a long-lived process.
    """

    return os.getenv("ENVIRONMENT", "development").strip().lower() == "production"


def use_query_manager() -> bool:
    """Route chat input through the structured ``QueryManager`` path.

    Default on. Clearing ``USE_QUERY_MANAGER`` falls back to the legacy
    single-handler search path, which still returns structured outcomes.
    """

    return _env_bool("USE_QUERY_MANAGER", True)


def use_structured_presenter() -> bool:
    """Render failures through the rich :mod:`ui.error_presenter` mapping.

    Default on. Clearing ``USE_STRUCTURED_PRESENTER`` is the presenter
    kill-switch: presentation collapses to a single static, user-safe notice so
    a bug in outcome presentation can never become an unhandled-exception
    surface.
    """

    return _env_bool("USE_STRUCTURED_PRESENTER", True)


def snapshot() -> dict[str, bool]:
    """Return the current flag values for operator diagnostics."""

    return {
        "is_production": is_production(),
        "use_query_manager": use_query_manager(),
        "use_structured_presenter": use_structured_presenter(),
    }


__all__ = [
    "is_production",
    "snapshot",
    "use_query_manager",
    "use_structured_presenter",
]
