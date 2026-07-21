#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Production-gated fault injection for non-production rollout testing (Phase 5).

Phase 5 requires running fault injection in a non-production environment against
the OpenAI, Pinecone, Redis, auth-callback, registry, queue, and rollback paths
so the structured-outcome machinery can be proven to classify real dependency
failures correctly before rollout.

This module is the mechanism. Named seams in the code call :func:`maybe_fail`;
normally that is a no-op. An operator (or a test) *arms* a seam so the next call
raises a chosen exception, which then flows through the existing typed
error-handling and surfaces as ``partial``/``unavailable``/etc.

Safety is layered and fail-closed:

* Arming is refused when ``ENVIRONMENT=production``.
* Even if a seam were somehow left armed, :func:`maybe_fail` is a hard no-op in
  production, so injected faults can never reach real users.
* The environment auto-arm (``FAULT_INJECTION``) is only honoured outside
  production.

The production check reads the environment live so a long-lived process cannot
be fooled by an import-time snapshot, and this module deliberately avoids
importing higher application layers so any seam can call it without a cycle.
"""

from __future__ import annotations

import os
import threading
from enum import Enum
from typing import Optional, Union


class FaultPoint(str, Enum):
    """Named seams where a fault can be injected during rollout testing."""

    OPENAI_EMBEDDING = "openai_embedding"
    OPENAI_ANSWER = "openai_answer"
    PINECONE_INDEX_OPEN = "pinecone_index_open"
    PINECONE_QUERY = "pinecone_query"
    REDIS = "redis"
    AUTH_CALLBACK = "auth_callback"
    REGISTRY_WRITE = "registry_write"
    QUEUE_DRAIN = "queue_drain"
    FRA_ROLLBACK = "fra_rollback"


ExceptionSpec = Union[BaseException, type]


def is_production() -> bool:
    """Return whether the process is running in a production deployment."""

    return os.getenv("ENVIRONMENT", "development").strip().lower() == "production"


class FaultInjectionDisabled(RuntimeError):
    """Raised when arming is attempted in a production deployment."""


class _ArmedFault:
    __slots__ = ("exception", "remaining")

    def __init__(self, exception: ExceptionSpec, remaining: Optional[int]) -> None:
        self.exception = exception
        self.remaining = remaining  # None => unlimited until disarmed


def _build_exception(spec: ExceptionSpec, point: "FaultPoint") -> BaseException:
    if isinstance(spec, BaseException):
        return spec
    if isinstance(spec, type) and issubclass(spec, BaseException):
        return spec(f"injected fault: {point.value}")
    raise TypeError(f"Unsupported fault exception spec: {spec!r}")


class FaultInjector:
    """Thread-safe registry of armed fault seams."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed: dict[FaultPoint, _ArmedFault] = {}

    def arm(
        self,
        point: FaultPoint,
        exception: ExceptionSpec = RuntimeError,
        count: Optional[int] = None,
    ) -> None:
        """Arm ``point`` so the next ``maybe_fail`` call(s) raise ``exception``.

        ``count`` bounds how many times the fault fires (``None`` => until
        disarmed). Refuses to arm in production.
        """

        if is_production():
            raise FaultInjectionDisabled(
                "Fault injection cannot be armed in a production deployment."
            )
        if count is not None and count <= 0:
            raise ValueError("count must be positive or None")
        with self._lock:
            self._armed[point] = _ArmedFault(exception, count)

    def disarm(self, point: FaultPoint) -> None:
        with self._lock:
            self._armed.pop(point, None)

    def clear(self) -> None:
        with self._lock:
            self._armed.clear()

    def is_armed(self, point: FaultPoint) -> bool:
        with self._lock:
            return point in self._armed

    def armed_points(self) -> list[str]:
        with self._lock:
            return sorted(point.value for point in self._armed)

    def maybe_fail(self, point: FaultPoint) -> None:
        """Raise the armed exception for ``point`` if one is due.

        Always a no-op in production. Outside production, if ``point`` is armed
        this decrements its remaining budget and raises the configured
        exception.
        """

        if is_production():
            return
        with self._lock:
            armed = self._armed.get(point)
            if armed is None:
                return
            if armed.remaining is not None:
                armed.remaining -= 1
                if armed.remaining <= 0:
                    del self._armed[point]
            exception_spec = armed.exception
        raise _build_exception(exception_spec, point)


_injector = FaultInjector()


def get_fault_injector() -> FaultInjector:
    """Return the process-wide fault injector."""

    return _injector


def maybe_fail(point: FaultPoint) -> None:
    """Module-level convenience over the process-wide injector."""

    _injector.maybe_fail(point)


def configure_from_env() -> list[str]:
    """Arm seams listed in ``FAULT_INJECTION`` (comma-separated) outside prod.

    Returns the list of points armed. In production this is a no-op and returns
    an empty list. Unknown tokens are ignored so a typo cannot break startup.
    """

    if is_production():
        return []
    raw = os.getenv("FAULT_INJECTION", "").strip()
    if not raw:
        return []
    armed: list[str] = []
    valid = {point.value: point for point in FaultPoint}
    for token in raw.split(","):
        name = token.strip().lower()
        point = valid.get(name)
        if point is not None:
            _injector.arm(point)
            armed.append(point.value)
    return armed


# Honour the environment auto-arm on import (no-op in production / when unset).
configure_from_env()


__all__ = [
    "FaultInjectionDisabled",
    "FaultInjector",
    "FaultPoint",
    "configure_from_env",
    "get_fault_injector",
    "is_production",
    "maybe_fail",
]
