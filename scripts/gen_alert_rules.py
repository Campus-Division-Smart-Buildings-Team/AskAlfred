#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regenerate the Prometheus alert-rules artifact from :mod:`core.alerts`.

Run from the repository root::

    python scripts/gen_alert_rules.py

This keeps ``ops/askalfred_alerts.yml`` in sync with the single source of truth
in :mod:`core.alerts`. A test asserts the checked-in artifact matches the
generated output so drift fails CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.alerts import render_prometheus_rules  # noqa: E402

HEADER = (
    "# AskAlfred outcome alert rules for Prometheus/Alertmanager.\n"
    "# Generated from core.alerts.render_prometheus_rules(); regenerate with\n"
    "# scripts/gen_alert_rules.py after changing the rules. Do not edit by hand.\n"
)

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "ops" / "askalfred_alerts.yml"


def build_artifact() -> str:
    """Return the full artifact text (header + rendered rules)."""

    return HEADER + render_prometheus_rules()


def main() -> int:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(build_artifact())
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
