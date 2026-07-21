#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare request-outcome rates against a baseline (Phase 5 rollout tool).

During rollout, capture a telemetry snapshot before the change (the baseline)
and after (the current window), then run::

    python tools/compare_outcome_rates.py --baseline baseline.json --current current.json

Each snapshot is the JSON dump of ``core.telemetry.get_telemetry().snapshot()``
(the flat ``"metric{labels}" -> count`` mapping). The tool reports the
empty/partial/unavailable/failed/degraded shares for both windows and exits
non-zero if any watched status rose materially above baseline, so it can gate a
rollout step in CI or a deploy script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as a script from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.outcome_rates import (  # noqa: E402
    DEFAULT_MIN_VOLUME,
    DEFAULT_RATE_INCREASE_TOLERANCE,
    DEFAULT_WATCHED_STATUSES,
    compare_to_baseline,
    outcome_counts,
    outcome_rates,
    total_requests,
)

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_USAGE = 2


def _load_snapshot(path: str) -> dict[str, int]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Snapshot {path} is not a JSON object")
    return {str(key): int(value) for key, value in data.items()}


def _format_rate_table(counts: dict[str, int]) -> str:
    rates = outcome_rates(counts)
    total = total_requests(counts)
    lines = [f"  total requests: {total}"]
    for status in sorted(set(list(counts) + list(DEFAULT_WATCHED_STATUSES))):
        lines.append(
            f"    {status:<22} count={counts.get(status, 0):<6} "
            f"rate={rates.get(status, 0.0):.3f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Baseline snapshot JSON")
    parser.add_argument("--current", required=True, help="Current snapshot JSON")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_RATE_INCREASE_TOLERANCE,
        help="Max allowed increase in a watched status share (default %(default)s)",
    )
    parser.add_argument(
        "--min-volume",
        type=int,
        default=DEFAULT_MIN_VOLUME,
        help="Minimum current-window requests before comparing (default %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        baseline_counts = outcome_counts(_load_snapshot(args.baseline))
        current_counts = outcome_counts(_load_snapshot(args.current))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE

    print("Baseline:")
    print(_format_rate_table(baseline_counts))
    print("Current:")
    print(_format_rate_table(current_counts))

    regressions = compare_to_baseline(
        current_counts,
        baseline_counts,
        tolerance=args.tolerance,
        min_volume=args.min_volume,
    )

    if not regressions:
        print("\nNo outcome-rate regression above tolerance.")
        return EXIT_OK

    print("\nRegressions detected (status: baseline -> current, +delta):")
    for regression in regressions:
        print(
            f"  {regression.status}: {regression.baseline_rate:.3f} -> "
            f"{regression.current_rate:.3f} (+{regression.delta:.3f})"
        )
    return EXIT_REGRESSION


if __name__ == "__main__":
    raise SystemExit(main())
