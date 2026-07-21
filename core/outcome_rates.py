#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compute and compare request-outcome rates against a baseline (Phase 5).

Phase 5 asks operators to "compare empty/partial/unavailable rates against
baseline" during rollout so a regression (e.g. the new contract turning healthy
results into ``empty``, or masking outages) is caught quickly.

This module holds the pure computation so it is trivially testable and reusable
by :mod:`tools.compare_outcome_rates`:

* :func:`outcome_counts` extracts per-status request counts from a
  :class:`~core.telemetry.Telemetry` snapshot (the flat
  ``"metric{labels}" -> count`` mapping).
* :func:`outcome_rates` turns counts into per-status shares of total traffic.
* :func:`compare_to_baseline` flags statuses whose share rose materially above
  the baseline.

Nothing here reads exception text or high-cardinality data; it only aggregates
the low-cardinality ``status`` label already enforced by telemetry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from core.outcomes import OutcomeStatus
from core.telemetry import METRIC_REQUEST_OUTCOME

# Statuses watched by default during rollout: a rise in any of these versus
# baseline is worth an operator's attention.
DEFAULT_WATCHED_STATUSES: tuple[str, ...] = (
    OutcomeStatus.EMPTY.value,
    OutcomeStatus.PARTIAL.value,
    OutcomeStatus.UNAVAILABLE.value,
    OutcomeStatus.FAILED.value,
    OutcomeStatus.DEGRADED.value,
)

# A watched status must rise by at least this share of traffic over baseline to
# count as a regression, and the current window needs at least this many
# requests before a ratio is trusted.
DEFAULT_RATE_INCREASE_TOLERANCE = 0.05
DEFAULT_MIN_VOLUME = 20

_LABELS_RE = re.compile(r"^(?P<metric>[a-z0-9_]+)(?:\{(?P<labels>.*)\})?$")


def _parse_labels(label_text: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not label_text:
        return labels
    for pair in label_text.split(","):
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        labels[name.strip()] = value.strip().strip('"')
    return labels


def outcome_counts(snapshot: Mapping[str, int]) -> dict[str, int]:
    """Return ``status -> count`` from a telemetry snapshot mapping.

    Accepts both the raw (``request_outcome_total``) and exporter-prefixed
    (``askalfred_request_outcome_total``) metric names, and sums across any
    other labels (e.g. failure ``code``) so each status has a single total.
    """

    counts: dict[str, int] = {}
    for key, value in snapshot.items():
        match = _LABELS_RE.match(key)
        if not match:
            continue
        metric = match.group("metric")
        if metric not in (METRIC_REQUEST_OUTCOME, f"askalfred_{METRIC_REQUEST_OUTCOME}"):
            continue
        labels = _parse_labels(match.group("labels") or "")
        status = labels.get("status")
        if status is None:
            continue
        counts[status] = counts.get(status, 0) + int(value)
    return counts


def total_requests(counts: Mapping[str, int]) -> int:
    """Return the total request volume across all statuses."""

    return sum(counts.values())


def outcome_rates(counts: Mapping[str, int]) -> dict[str, float]:
    """Return ``status -> share`` of total requests (empty mapping if no data)."""

    total = total_requests(counts)
    if total <= 0:
        return {}
    return {status: count / total for status, count in counts.items()}


@dataclass(frozen=True)
class RateRegression:
    """A watched status whose share rose materially above baseline."""

    status: str
    baseline_rate: float
    current_rate: float

    @property
    def delta(self) -> float:
        return self.current_rate - self.baseline_rate


def compare_to_baseline(
    current: Mapping[str, int],
    baseline: Mapping[str, int],
    *,
    watched_statuses: Iterable[str] = DEFAULT_WATCHED_STATUSES,
    tolerance: float = DEFAULT_RATE_INCREASE_TOLERANCE,
    min_volume: int = DEFAULT_MIN_VOLUME,
) -> list[RateRegression]:
    """Return watched statuses whose current share exceeds baseline + tolerance.

    ``current`` and ``baseline`` are ``status -> count`` mappings (see
    :func:`outcome_counts`). When the current window has fewer than
    ``min_volume`` requests the comparison is skipped (returns no regressions),
    because a ratio over a tiny sample is noise.
    """

    if total_requests(current) < min_volume:
        return []
    current_rates = outcome_rates(current)
    baseline_rates = outcome_rates(baseline)
    regressions: list[RateRegression] = []
    for status in watched_statuses:
        current_rate = current_rates.get(status, 0.0)
        baseline_rate = baseline_rates.get(status, 0.0)
        if current_rate - baseline_rate > tolerance:
            regressions.append(
                RateRegression(
                    status=status,
                    baseline_rate=baseline_rate,
                    current_rate=current_rate,
                )
            )
    return regressions


__all__ = [
    "DEFAULT_MIN_VOLUME",
    "DEFAULT_RATE_INCREASE_TOLERANCE",
    "DEFAULT_WATCHED_STATUSES",
    "RateRegression",
    "compare_to_baseline",
    "outcome_counts",
    "outcome_rates",
    "total_requests",
]
