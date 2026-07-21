"""Phase 0 acceptance-inventory and silent-failure baseline tests."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.failure_acceptance import P0_P1_FAILURE_ACCEPTANCE
from core.failure_codes import FAILURE_CODE_SPECS, FailureCode
from core.outcomes import OutcomeStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = REPO_ROOT / "plan" / "failure_and_degraded_states_plan.md"

# Phase 0 freezes the existing broad-exception/silent-sentinel debt so later
# phases can remove it incrementally. Any new fingerprint fails this test.
#
# Phase 2 removed the semantic and structured retrieval entries as those paths
# migrated to typed source outcomes (search_one_index_with_outcome,
# _query_index_with_outcome, and per-source embedding outcomes). The best-effort
# `_query_index_with_batches` wrapper still returns [] and remains frozen.
#
# Phase 3 removed the rate-limiter lease entries: RedisRateLimiter.acquire_lease
# and release_lease now fail closed (return False) instead of returning a
# nominal-success sentinel, and emit a degraded-service metric (START-06).
SILENT_FAILURE_BASELINE = {
    "auth/auth_manager.py:_try_complete_authentication",
    "building/alias_override.py:validate_overrides",
    "building/path_inventory_summary.py:_is_binary_file",
    "core/date_utils.py:_fetch_document_chunks",
    "core/date_utils.py:extract_date_from_single_result",
    "core/date_utils.py:parse_date_to_iso",
    "core/env_bootstrap.py:load_local_env",
    "core/pinecone_utils.py:list_index_names",
    "core/pinecone_utils.py:query_all_chunks",
    "ingest/document_content.py:extract_maintenance_csv",
    "ingest/upsert_handler.py:Dispatcher._execute_inline",
    "search_core/structured_queries.py:_query_index_with_batches",
}


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
def test_p0_p1_state_contract(state_id, contract):
    """Own every P0/P1 row with a stable, low-cardinality outcome contract."""

    assert re.fullmatch(r"[A-Z]+-\d+", state_id)
    assert contract.priority in {"P0", "P1"}
    assert isinstance(contract.status, OutcomeStatus)
    assert isinstance(contract.code, FailureCode)
    assert contract.code in FAILURE_CODE_SPECS
    assert re.fullmatch(r"[a-z][a-z0-9_]*", contract.component)
    assert contract.owning_test == (
        "tests/test_failure_acceptance_inventory.py::"
        f"test_p0_p1_state_contract[{state_id}]"
    )


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


def test_no_new_broad_exception_path_returns_a_silent_sentinel():
    current = _silent_failure_fingerprints()
    additions = current - SILENT_FAILURE_BASELINE

    assert not additions, (
        "New broad exception paths must return an explicit degraded/failure "
        f"outcome instead of a silent sentinel: {sorted(additions)}"
    )
