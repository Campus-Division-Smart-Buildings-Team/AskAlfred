#!/usr/bin/env python3
"""Validate deployment evidence required to close the failure-state backlog."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

REQUIRED_FAULT_SEAMS = frozenset(
    {
        "openai_embedding",
        "openai_answer",
        "pinecone_index_open",
        "pinecone_query",
        "redis",
        "auth_callback",
        "registry_write",
        "queue_drain",
        "fra_rollback",
    }
)


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_timestamp(value: object) -> bool:
    if not _has_text(value):
        return False
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_evidence(payload: object) -> list[str]:
    """Return deterministic validation errors; an empty list means complete."""

    errors: list[str] = []
    root = _mapping(payload)
    if not root:
        return ["manifest must be a JSON object"]

    environment = root.get("environment")
    if not _has_text(environment) or environment == "non-production-environment-name":
        errors.append("environment must identify the target deployment")
    if not _valid_timestamp(root.get("captured_at")):
        errors.append("captured_at must be an ISO-8601 timestamp")

    monitoring = _mapping(root.get("monitoring"))
    for component in ("prometheus", "alertmanager", "grafana"):
        if monitoring.get(f"{component}_connected") is not True:
            errors.append(f"monitoring.{component}_connected must be true")
    if not _has_text(monitoring.get("evidence_ref")):
        errors.append("monitoring.evidence_ref is required")

    faults = _mapping(root.get("fault_injection"))
    missing = REQUIRED_FAULT_SEAMS - set(faults)
    extra = set(faults) - REQUIRED_FAULT_SEAMS
    if missing:
        errors.append(f"fault_injection is missing seams: {', '.join(sorted(missing))}")
    if extra:
        errors.append(f"fault_injection has unknown seams: {', '.join(sorted(extra))}")
    for seam in sorted(REQUIRED_FAULT_SEAMS & set(faults)):
        result = _mapping(faults[seam])
        if result.get("passed") is not True:
            errors.append(f"fault_injection.{seam}.passed must be true")
        if not _has_text(result.get("evidence_ref")):
            errors.append(f"fault_injection.{seam}.evidence_ref is required")

    baseline = _mapping(root.get("traffic_baseline"))
    for field in ("baseline_snapshot", "current_snapshot", "evidence_ref"):
        if not _has_text(baseline.get(field)):
            errors.append(f"traffic_baseline.{field} is required")
    if baseline.get("comparison_passed") is not True:
        errors.append("traffic_baseline.comparison_passed must be true")

    approvals = _mapping(root.get("approvals"))
    for approval_name in ("user_copy", "alert_thresholds"):
        approval = _mapping(approvals.get(approval_name))
        if approval.get("approved") is not True:
            errors.append(f"approvals.{approval_name}.approved must be true")
        if not _valid_timestamp(approval.get("approved_at")):
            errors.append(
                f"approvals.{approval_name}.approved_at must be an ISO-8601 timestamp"
            )
        if not _has_text(approval.get("evidence_ref")):
            errors.append(f"approvals.{approval_name}.evidence_ref is required")

    acl = _mapping(root.get("acl"))
    threshold = acl.get("threshold")
    measured = acl.get("measured_conformance")
    if not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
        errors.append("acl.threshold must be between 0 and 1")
    if not isinstance(measured, (int, float)) or not 0 <= measured <= 1:
        errors.append("acl.measured_conformance must be between 0 and 1")
    elif isinstance(threshold, (int, float)) and measured < threshold:
        errors.append("acl.measured_conformance is below acl.threshold")
    if acl.get("remediation_complete") is not True:
        errors.append("acl.remediation_complete must be true")
    if not _has_text(acl.get("evidence_ref")):
        errors.append("acl.evidence_ref is required")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"INVALID: could not read evidence manifest: {error}")
        return 2

    errors = validate_evidence(payload)
    if errors:
        print("INCOMPLETE rollout evidence:")
        for error in errors:
            print(f"- {error}")
        return 2
    print("COMPLETE: all rollout evidence criteria are satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
