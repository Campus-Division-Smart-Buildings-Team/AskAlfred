"""START-09 / START-10: startup dependency-configuration readiness checks.

Covers the completion of the dependency readiness checks:

* Required OpenAI/Pinecone and optional Redis configuration is validated once at
  startup and published as component readiness.
* Each required and optional component publishes a readiness state; a missing
  required dependency is ``unavailable`` while an invalid *optional* Redis
  configuration is only ``degraded`` (query rate limiting fails open).
* A missing required dependency maps to a typed ``unavailable`` query outcome
  before the query executes.
* The detailed configuration cause stays in operator diagnostics/logs and never
  reaches the readiness surface or the user-facing outcome.
"""

from __future__ import annotations

import pytest

from core.failure_codes import FailureCode
from core.outcomes import OutcomeStatus
from core.startup_readiness import (
    REQUIRED_QUERY_DEPENDENCIES,
    check_dependency_readiness,
    missing_required_query_dependency,
)
from core.telemetry import (
    COMPONENT_OPENAI,
    COMPONENT_PINECONE,
    COMPONENT_REDIS,
    METRIC_SERVICE_DEGRADED,
    Readiness,
    ReadinessRegistry,
    Telemetry,
    get_readiness,
    get_telemetry,
)
from query_core.query_manager import QueryManager
from query_core.query_result import QueryResult
from query_core.query_route import QueryRoute


@pytest.fixture(autouse=True)
def _reset_process_state():
    """Isolate the process-wide telemetry/readiness singletons per test."""
    get_telemetry().reset()
    get_readiness().reset()
    yield
    get_telemetry().reset()
    get_readiness().reset()


@pytest.fixture
def all_dependencies_configured(monkeypatch):
    """Set a fully valid configuration for every startup dependency."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("PINECONE_API_KEY", "pcne-test-pinecone")
    monkeypatch.setenv("REDIS_HOST", "redis.example.test")
    monkeypatch.setenv("REDIS_PORT", "6379")
    # Clear any timeout overrides so validation uses the safe defaults.
    for name in (
        "REDIS_SOCKET_TIMEOUT",
        "REDIS_SOCKET_CONNECT_TIMEOUT",
        "REDIS_HEALTH_CHECK_INTERVAL",
        "REDIS_DB",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# check_dependency_readiness
# ---------------------------------------------------------------------------


def test_all_configured_publishes_ready_for_every_component(
    all_dependencies_configured,
):
    readiness = ReadinessRegistry()
    telemetry = Telemetry()

    results = check_dependency_readiness(readiness=readiness, telemetry=telemetry)

    by_component = {r.component: r for r in results}
    assert set(by_component) == {
        COMPONENT_OPENAI,
        COMPONENT_PINECONE,
        COMPONENT_REDIS,
    }
    for component in (COMPONENT_OPENAI, COMPONENT_PINECONE, COMPONENT_REDIS):
        assert readiness.get(component) is Readiness.READY
        assert by_component[component].readiness is Readiness.READY
        assert by_component[component].failure_code is None

    # No degraded-service events when everything is configured.
    assert telemetry.get(
        METRIC_SERVICE_DEGRADED,
        component=COMPONENT_OPENAI,
        code=FailureCode.CONFIGURATION_INVALID,
    ) == 0
    assert missing_required_query_dependency(readiness) is None


def test_required_openai_missing_is_unavailable(
    all_dependencies_configured, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    readiness = ReadinessRegistry()
    telemetry = Telemetry()

    results = check_dependency_readiness(readiness=readiness, telemetry=telemetry)
    openai_result = next(r for r in results if r.component == COMPONENT_OPENAI)

    assert readiness.get(COMPONENT_OPENAI) is Readiness.UNAVAILABLE
    assert openai_result.readiness is Readiness.UNAVAILABLE
    assert openai_result.required_for_query is True
    assert openai_result.failure_code is FailureCode.CONFIGURATION_INVALID
    # Pinecone remained configured and ready.
    assert readiness.get(COMPONENT_PINECONE) is Readiness.READY
    # The outage is observable as a degraded-service event.
    assert telemetry.get(
        METRIC_SERVICE_DEGRADED,
        component=COMPONENT_OPENAI,
        code=FailureCode.CONFIGURATION_INVALID,
    ) == 1
    assert missing_required_query_dependency(readiness) == COMPONENT_OPENAI


def test_required_pinecone_missing_is_unavailable(
    all_dependencies_configured, monkeypatch
):
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    readiness = ReadinessRegistry()

    check_dependency_readiness(readiness=readiness, telemetry=Telemetry())

    assert readiness.get(COMPONENT_PINECONE) is Readiness.UNAVAILABLE
    assert missing_required_query_dependency(readiness) == COMPONENT_PINECONE


def test_optional_redis_invalid_port_is_degraded_not_unavailable(
    all_dependencies_configured, monkeypatch
):
    monkeypatch.setenv("REDIS_PORT", "not-a-port")
    readiness = ReadinessRegistry()
    telemetry = Telemetry()

    results = check_dependency_readiness(readiness=readiness, telemetry=telemetry)
    redis_result = next(r for r in results if r.component == COMPONENT_REDIS)

    # Redis is optional for the query path (rate limiting fails open), so an
    # invalid configuration degrades rather than blocks the query surface.
    assert readiness.get(COMPONENT_REDIS) is Readiness.DEGRADED
    assert redis_result.readiness is Readiness.DEGRADED
    assert redis_result.required_for_query is False
    assert telemetry.get(
        METRIC_SERVICE_DEGRADED,
        component=COMPONENT_REDIS,
        code=FailureCode.CONFIGURATION_INVALID,
    ) == 1
    # An optional dependency never appears as a missing *required* dependency.
    assert missing_required_query_dependency(readiness) is None


def test_optional_redis_missing_host_is_degraded(
    all_dependencies_configured, monkeypatch
):
    monkeypatch.delenv("REDIS_HOST", raising=False)
    readiness = ReadinessRegistry()

    check_dependency_readiness(readiness=readiness, telemetry=Telemetry())

    assert readiness.get(COMPONENT_REDIS) is Readiness.DEGRADED


def test_configuration_cause_stays_out_of_readiness_surface(
    all_dependencies_configured, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    readiness = ReadinessRegistry()

    results = check_dependency_readiness(readiness=readiness, telemetry=Telemetry())

    # The operator-facing detail names the variable, but only for diagnostics.
    openai_result = next(r for r in results if r.component == COMPONENT_OPENAI)
    assert "OPENAI_API_KEY" in openai_result.detail

    # The published readiness snapshot carries only the coarse state and stable
    # failure code -- never the detailed cause.
    snapshot = readiness.snapshot()[COMPONENT_OPENAI]
    assert snapshot == {
        "readiness": Readiness.UNAVAILABLE.value,
        "code": FailureCode.CONFIGURATION_INVALID.value,
    }
    assert "OPENAI_API_KEY" not in str(snapshot)


def test_check_uses_process_singletons_by_default(
    all_dependencies_configured, monkeypatch
):
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)

    check_dependency_readiness()

    # With no explicit registries the process-wide singletons are published.
    assert get_readiness().get(COMPONENT_PINECONE) is Readiness.UNAVAILABLE
    assert missing_required_query_dependency() == COMPONENT_PINECONE


def test_required_query_dependencies_are_openai_and_pinecone():
    assert set(REQUIRED_QUERY_DEPENDENCIES) == {
        COMPONENT_OPENAI,
        COMPONENT_PINECONE,
    }


# ---------------------------------------------------------------------------
# Query-path gate (START-09): map a missing required dependency to unavailable
# ---------------------------------------------------------------------------


def test_process_query_unavailable_when_required_dependency_missing(monkeypatch):
    # Isolate from the deployment's auth posture: the access check runs before
    # the dependency gate, so allow the (anonymous) request through to it.
    monkeypatch.setattr(
        "query_core.query_manager.auth_is_mandatory", lambda: False
    )
    # A required dependency was found unconfigured at startup.
    get_readiness().mark_unavailable(
        COMPONENT_OPENAI, FailureCode.CONFIGURATION_INVALID
    )
    manager = QueryManager(intent_classifier=object())

    # Prove the gate short-circuits *before* query execution: routing must never
    # be reached.
    def fail_if_routed(_self, _context):
        raise AssertionError("routing must not run when a dependency is missing")

    monkeypatch.setattr(QueryManager, "_route_query_hybrid", fail_if_routed)

    result = manager.process_query(
        "What is the BMS configuration for HVAC?",
        authenticated=True,
        tenant_id="tenant-test",
        user_roles=("reader",),
    )

    assert result.status is OutcomeStatus.UNAVAILABLE
    assert result.failure is not None
    assert result.failure.code is FailureCode.DEPENDENCY_UNAVAILABLE
    assert result.failure.retryable is True
    assert result.failure.safe_context == {"dependency": COMPONENT_OPENAI}
    assert result.answer is None
    assert result.results == []
    assert result.metadata == {"route": "dependency_unavailable"}


def test_process_query_proceeds_when_dependencies_ready(monkeypatch):
    # All required dependencies ready (default readiness) -> gate is a no-op and
    # routing runs as usual.
    manager = QueryManager(intent_classifier=object())
    routed = {"called": False}

    def stub_route(_self, context):
        routed["called"] = True
        handler = manager.handlers[0]
        return QueryRoute(handler=handler, metadata={"route": "stub"})

    monkeypatch.setattr(QueryManager, "_route_query_hybrid", stub_route)
    monkeypatch.setattr(
        QueryManager, "_run_preprocessors", lambda self, context: None
    )
    monkeypatch.setattr(
        QueryManager,
        "_record_building_directory_readiness",
        lambda self, context: False,
    )
    monkeypatch.setattr(
        manager.handlers[0],
        "handle",
        lambda context: QueryResult(
            query=context.query, answer="ok", status=OutcomeStatus.SUCCESS
        ),
    )

    result = manager.process_query(
        "hello there",
        authenticated=True,
        tenant_id="tenant-test",
        user_roles=("reader",),
    )

    assert routed["called"] is True
    assert result.status is OutcomeStatus.SUCCESS
