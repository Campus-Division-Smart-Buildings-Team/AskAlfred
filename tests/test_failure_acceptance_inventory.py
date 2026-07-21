"""Phase 0 acceptance-inventory and silent-failure baseline tests."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.failure_acceptance import P0_P1_FAILURE_ACCEPTANCE
from core.failure_codes import FAILURE_CODE_SPECS, FailureCode, get_failure_code_spec
from core.outcomes import FailureInfo, OutcomeStatus
from core.telemetry import METRIC_REQUEST_OUTCOME, Telemetry
from ui.error_presenter import present_outcome

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = REPO_ROOT / "plan" / "failure_and_degraded_states_plan.md"

# Phase 5 completion requires this baseline to stay empty. Broad exception
# handlers may recover, but must do so through an explicit typed/degraded path
# rather than returning an empty/None/nominal-success sentinel.
SILENT_FAILURE_BASELINE: set[str] = set()


def _registered_p0_p1_rows() -> dict[str, str]:
    source = REGISTER_PATH.read_text(encoding="utf-8")
    return {
        match.group("state_id"): match.group("priority")
        for match in re.finditer(
            r"^\| (?P<state_id>[A-Z]+-\d+) \| "
            r"(?P<priority>P[01]) \|",
            source,
            flags=re.MULTILINE,
        )
    }


def test_acceptance_inventory_exactly_covers_register_p0_p1_rows():
    register_rows = _registered_p0_p1_rows()

    assert register_rows
    assert set(P0_P1_FAILURE_ACCEPTANCE) == set(register_rows)
    assert {
        state_id: contract.priority
        for state_id, contract in P0_P1_FAILURE_ACCEPTANCE.items()
    } == register_rows


@pytest.mark.parametrize(
    ("state_id", "contract"),
    [
        pytest.param(state_id, contract, id=state_id)
        for state_id, contract in sorted(P0_P1_FAILURE_ACCEPTANCE.items())
    ],
)
def test_p0_p1_failure_behaviour(state_id, contract):
    """Exercise every P0/P1 failure through outcome, telemetry and UI layers."""

    assert re.fullmatch(r"[A-Z]+-\d+", state_id)
    assert contract.priority in {"P0", "P1"}
    assert isinstance(contract.status, OutcomeStatus)
    assert isinstance(contract.code, FailureCode)
    assert contract.code in FAILURE_CODE_SPECS
    assert re.fullmatch(r"[a-z][a-z0-9_]*", contract.component)
    assert contract.owning_test == (
        "tests/test_failure_acceptance_inventory.py::"
        f"test_p0_p1_failure_behaviour[{state_id}]"
    )

    # Inject the registered named failure into the shared operation boundary.
    # This verifies more than schema ownership: terminal status, stable code,
    # retryability, low-cardinality telemetry, and safe user treatment all run.
    failure = FailureInfo.from_code(contract.code, contract.component)
    assert failure.code is contract.code
    assert failure.retryable is get_failure_code_spec(contract.code).retryable

    telemetry = Telemetry()
    telemetry.record_request_outcome(contract.status, contract.code)
    assert telemetry.get(
        METRIC_REQUEST_OUTCOME,
        status=contract.status,
        code=contract.code,
    ) == 1

    presented = present_outcome(contract.status, failure)
    assert presented.render_as_notice is (contract.status is not OutcomeStatus.SUCCESS)
    assert presented.severity in {"info", "warning", "error"}
    assert presented.message
    assert failure.correlation_id == presented.reference
    assert state_id not in presented.message


def _is_empty_or_nominal_success_return(node: ast.Return) -> bool:
    value = node.value
    if value is None:
        return True
    if isinstance(value, ast.Constant):
        return value.value is None or value.value is True
    if isinstance(value, (ast.List, ast.Tuple)):
        return not value.elts
    if isinstance(value, ast.Dict):
        return not value.keys
    return False


def _returns_nominal_query_success(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Name) or node.func.id != "QueryResult":
        return False
    return any(
        keyword.arg == "success"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in node.keywords
    )


class _SilentFailureVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.scope: list[str] = []
        self.fingerprints: set[str] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Try(self, node: ast.Try) -> None:
        for handler in node.handlers:
            broad = handler.type is None or (
                isinstance(handler.type, ast.Name)
                and handler.type.id in {"Exception", "BaseException"}
            )
            if not broad:
                continue
            handler_tree = ast.Module(body=handler.body, type_ignores=[])
            silently_returns = any(
                isinstance(child, ast.Return)
                and _is_empty_or_nominal_success_return(child)
                or isinstance(child, ast.Call)
                and _returns_nominal_query_success(child)
                for child in ast.walk(handler_tree)
            )
            if silently_returns:
                scope = ".".join(self.scope) or "<module>"
                self.fingerprints.add(f"{self.relative_path}:{scope}")
        self.generic_visit(node)


def _silent_failure_fingerprints() -> set[str]:
    roots = (
        "main.py",
        "auth",
        "query_core",
        "query_handlers",
        "search_core",
        "building",
        "security",
        "core",
        "interfaces",
        "ingest",
        "fra",
        "ui",
    )
    paths: list[Path] = []
    for root in roots:
        path = REPO_ROOT / root
        paths.extend([path] if path.is_file() else path.rglob("*.py"))

    fingerprints: set[str] = set()
    for path in paths:
        relative_path = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        visitor = _SilentFailureVisitor(relative_path)
        visitor.visit(tree)
        fingerprints.update(visitor.fingerprints)
    return fingerprints


def test_silent_failure_baseline_is_empty():
    current = _silent_failure_fingerprints()
    assert current == SILENT_FAILURE_BASELINE == set(), (
        "Broad exception paths must return an explicit degraded/failure "
        f"outcome instead of a silent sentinel: {sorted(current)}"
    )
