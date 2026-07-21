"""Completion-gate tests for the operational rollout evidence manifest."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from tools.validate_rollout_evidence import REQUIRED_FAULT_SEAMS, validate_evidence

EXAMPLE = Path(__file__).resolve().parents[1] / "ops" / "rollout_evidence.example.json"


def _complete_manifest() -> dict:
    payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    payload["environment"] = "askalfred-staging"
    payload["monitoring"] = {
        "prometheus_connected": True,
        "alertmanager_connected": True,
        "grafana_connected": True,
        "evidence_ref": "change/monitoring-123",
    }
    payload["fault_injection"] = {
        seam: {"passed": True, "evidence_ref": f"run/{seam}"}
        for seam in REQUIRED_FAULT_SEAMS
    }
    payload["traffic_baseline"] = {
        "baseline_snapshot": "artifact/baseline.json",
        "current_snapshot": "artifact/current.json",
        "comparison_passed": True,
        "evidence_ref": "run/outcome-comparison",
    }
    payload["approvals"] = {
        name: {
            "approved": True,
            "approved_at": "2026-01-02T00:00:00Z",
            "evidence_ref": f"approval/{name}",
        }
        for name in ("user_copy", "alert_thresholds")
    }
    payload["acl"] = {
        "threshold": 1.0,
        "measured_conformance": 1.0,
        "remediation_complete": True,
        "evidence_ref": "audit/acl-123",
    }
    return payload


def test_example_is_deliberately_incomplete():
    payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    errors = validate_evidence(payload)
    assert errors
    assert any("environment" in error for error in errors)


def test_complete_manifest_satisfies_every_gate():
    assert validate_evidence(_complete_manifest()) == []


def test_every_fault_seam_requires_pass_and_evidence():
    payload = _complete_manifest()
    seam = sorted(REQUIRED_FAULT_SEAMS)[0]
    payload["fault_injection"][seam] = {"passed": False, "evidence_ref": ""}
    errors = validate_evidence(payload)
    assert f"fault_injection.{seam}.passed must be true" in errors
    assert f"fault_injection.{seam}.evidence_ref is required" in errors


def test_acl_must_meet_the_deployment_threshold():
    payload = copy.deepcopy(_complete_manifest())
    payload["acl"]["measured_conformance"] = 0.99
    assert "acl.measured_conformance is below acl.threshold" in validate_evidence(payload)
