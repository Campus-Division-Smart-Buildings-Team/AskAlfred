#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Startup dependency-configuration readiness checks (plan START-09 / START-10).

At controlled startup we validate that each external dependency is *configured*
before any query runs, and publish coarse readiness for every component so an
operator view or health probe can report it without inspecting logs. Only the
configuration is validated here (credentials present, Redis host/port and
timeouts well-formed); no network client is constructed and no socket is opened,
so a controlled startup cannot hang on a dependency probe.

Dependencies are classified by the path that *requires* them (plan START-10 asks
that optional UI dependencies be separated from mandatory ingestion ones):

* OpenAI and Pinecone are REQUIRED for the query path: embeddings and vector
  retrieval cannot run without them. A missing/invalid configuration is published
  as ``unavailable`` and maps the query path to a typed ``unavailable`` outcome
  before execution (START-09).
* Redis is OPTIONAL for the query path (rate limiting fails open to an in-memory
  limiter) but REQUIRED for ingestion (distributed leases guard FRA
  exclusivity). An invalid Redis configuration is therefore published as
  ``degraded`` for the query surface, not ``unavailable``.

The detailed configuration cause (which variable is missing/invalid) is logged
and returned for operator diagnostics only; it never reaches the readiness
surface or a user-facing outcome (plan section H).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

from core.alfred_exceptions import ConfigError
from core.clients import validate_redis_config
from core.failure_codes import FailureCode
from core.telemetry import (
    COMPONENT_OPENAI,
    COMPONENT_PINECONE,
    COMPONENT_REDIS,
    Readiness,
    ReadinessRegistry,
    Telemetry,
    get_readiness,
    get_telemetry,
)
from security.log_sanitiser import sanitise_error

LOGGER = logging.getLogger(__name__)


def _validate_openai_config() -> None:
    """Raise :class:`ConfigError` when the OpenAI API key is not configured."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key or not key.strip():
        raise ConfigError("OPENAI_API_KEY is not configured")


def _validate_pinecone_config() -> None:
    """Raise :class:`ConfigError` when the Pinecone API key is not configured."""
    key = os.environ.get("PINECONE_API_KEY")
    if not key or not key.strip():
        raise ConfigError("PINECONE_API_KEY is not configured")


@dataclass(frozen=True)
class DependencySpec:
    """A startup-validated external dependency and its query-path requirement."""

    component: str
    required_for_query: bool
    validate: Callable[[], None]
    note: str


@dataclass(frozen=True)
class DependencyReadiness:
    """The published readiness of one dependency plus operator-only detail.

    ``detail`` is a sanitised, operator-facing string (no secrets); it is kept
    off the readiness surface and out of user-facing outcomes.
    """

    component: str
    readiness: Readiness
    required_for_query: bool
    failure_code: Optional[FailureCode]
    detail: str


# Ordered so the most fundamental dependencies are validated first.
DEPENDENCY_SPECS: tuple[DependencySpec, ...] = (
    DependencySpec(
        component=COMPONENT_OPENAI,
        required_for_query=True,
        validate=_validate_openai_config,
        note="required: embeddings and answer generation",
    ),
    DependencySpec(
        component=COMPONENT_PINECONE,
        required_for_query=True,
        validate=_validate_pinecone_config,
        note="required: vector retrieval",
    ),
    DependencySpec(
        component=COMPONENT_REDIS,
        required_for_query=False,
        validate=validate_redis_config,
        note="optional for query (rate limiting fails open); required for ingestion",
    ),
)

# Query-path dependencies whose absence must map a request to ``unavailable``.
REQUIRED_QUERY_DEPENDENCIES: tuple[str, ...] = tuple(
    spec.component for spec in DEPENDENCY_SPECS if spec.required_for_query
)


def check_dependency_readiness(
    *,
    readiness: ReadinessRegistry | None = None,
    telemetry: Telemetry | None = None,
    specs: tuple[DependencySpec, ...] = DEPENDENCY_SPECS,
) -> list[DependencyReadiness]:
    """Validate each dependency's configuration once and publish its readiness.

    Required dependencies (OpenAI, Pinecone) that are misconfigured are marked
    ``unavailable``; the optional Redis dependency is marked ``degraded`` because
    the query path can still fall back to an in-memory limiter. Every unready
    dependency also increments a low-cardinality degraded-service metric. The
    detailed cause is logged and returned for operator diagnostics only.

    Returns one :class:`DependencyReadiness` per spec, in spec order.
    """
    readiness = readiness or get_readiness()
    telemetry = telemetry or get_telemetry()

    results: list[DependencyReadiness] = []
    for spec in specs:
        try:
            spec.validate()
        except ConfigError as exc:
            detail = sanitise_error(exc)
            if spec.required_for_query:
                state = Readiness.UNAVAILABLE
                readiness.mark_unavailable(
                    spec.component, FailureCode.CONFIGURATION_INVALID
                )
            else:
                state = Readiness.DEGRADED
                readiness.mark_degraded(
                    spec.component, FailureCode.CONFIGURATION_INVALID
                )
            telemetry.record_service_degraded(
                spec.component, FailureCode.CONFIGURATION_INVALID
            )
            LOGGER.error(
                "startup_dependency_unready component=%s readiness=%s "
                "required_for_query=%s cause=%s",
                spec.component,
                state.value,
                spec.required_for_query,
                detail,
            )
            results.append(
                DependencyReadiness(
                    component=spec.component,
                    readiness=state,
                    required_for_query=spec.required_for_query,
                    failure_code=FailureCode.CONFIGURATION_INVALID,
                    detail=detail,
                )
            )
        else:
            readiness.mark_ready(spec.component)
            LOGGER.info("startup_dependency_ready component=%s", spec.component)
            results.append(
                DependencyReadiness(
                    component=spec.component,
                    readiness=Readiness.READY,
                    required_for_query=spec.required_for_query,
                    failure_code=None,
                    detail="configured",
                )
            )
    return results


def missing_required_query_dependency(
    readiness: ReadinessRegistry | None = None,
) -> Optional[str]:
    """Return the first required query dependency that is currently unavailable.

    Reads the published readiness so the query path can fail fast with a typed
    ``unavailable`` outcome instead of proceeding into a handler that would raise
    a ``ConfigError`` on first use. Returns the component name, or ``None`` when
    every required dependency is ready.
    """
    readiness = readiness or get_readiness()
    for component in REQUIRED_QUERY_DEPENDENCIES:
        if readiness.get(component) is Readiness.UNAVAILABLE:
            return component
    return None


__all__ = [
    "DEPENDENCY_SPECS",
    "REQUIRED_QUERY_DEPENDENCIES",
    "DependencyReadiness",
    "DependencySpec",
    "check_dependency_readiness",
    "missing_required_query_dependency",
]
