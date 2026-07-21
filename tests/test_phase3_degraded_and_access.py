"""Phase 3 tests: degraded services and access control.

Covers the five Phase 3 exit criteria:

- Query throttling degradation is observable (rate-limit backend fail-open emits
  a degraded-service metric and marks the component degraded).
- Integrity-critical leases never fail open (a Redis lease op that cannot confirm
  ownership fails closed).
- Missing tenant/role context cannot masquerade as normal no results, and an
  anonymous session in a mandatory-auth deployment fails closed before retrieval
  (AUTH-13).
- ACL conformance can be measured, and ACL-metadata drops are counted (AUTH-10).
- The anonymous/unfiltered retrieval path cannot expose ACL-restricted documents
  in mandatory-auth deployments (deny-all filter).

Plus the supporting telemetry contract: low-cardinality labels only, and the
component readiness registry.
"""

from __future__ import annotations

import pytest

from auth.access_control import (
    filter_authorized_matches,
    measure_acl_conformance,
)
from core.failure_codes import FailureCode
from core.outcomes import FailureInfo, OutcomeStatus
from core.telemetry import (
    COMPONENT_BUILDING_DIRECTORY,
    COMPONENT_INTENT_CLASSIFIER,
    COMPONENT_RATE_LIMITER,
    COMPONENT_RESOURCE_LEASE,
    METRIC_ACL_METADATA_DROP,
    METRIC_FALLBACK_ACTIVATED,
    METRIC_REQUEST_OUTCOME,
    METRIC_SERVICE_DEGRADED,
    Readiness,
    ReadinessRegistry,
    Telemetry,
    get_readiness,
    get_telemetry,
)
from query_core import query_context
from query_core.query_context import (
    DENY_ALL_TENANT_ID,
    QueryContext,
    auth_is_mandatory,
    build_access_filter,
    validate_access_context,
)
from query_core.query_manager import QueryManager
from query_core.query_result import QueryResult
from security.rate_limiter import (
    InMemoryRateLimiter,
    RateLimiterManager,
    RedisRateLimiter,
)


@pytest.fixture(autouse=True)
def _reset_process_state():
    """Isolate the process-wide telemetry/readiness singletons per test."""
    get_telemetry().reset()
    get_readiness().reset()
    yield
    get_telemetry().reset()
    get_readiness().reset()


# ---------------------------------------------------------------------------
# core/telemetry.py
# ---------------------------------------------------------------------------


def test_readiness_defaults_to_ready_until_recorded():
    registry = ReadinessRegistry()
    assert registry.get("never_seen") is Readiness.READY
    assert registry.is_healthy("never_seen")


def test_readiness_records_state_and_code_in_snapshot():
    registry = ReadinessRegistry()
    registry.mark_degraded(
        COMPONENT_RATE_LIMITER, FailureCode.RATE_LIMIT_BACKEND_UNAVAILABLE
    )
    assert registry.get(COMPONENT_RATE_LIMITER) is Readiness.DEGRADED
    assert not registry.is_healthy(COMPONENT_RATE_LIMITER)
    snap = registry.snapshot()
    assert snap[COMPONENT_RATE_LIMITER]["readiness"] == "degraded"
    assert snap[COMPONENT_RATE_LIMITER]["code"] == "rate_limit.backend_unavailable"


def test_telemetry_counts_and_snapshot_render_labels():
    tel = Telemetry()
    tel.increment(METRIC_REQUEST_OUTCOME, status="empty")
    tel.increment(METRIC_REQUEST_OUTCOME, status="empty")
    tel.increment(METRIC_REQUEST_OUTCOME, status="unavailable")
    assert tel.get(METRIC_REQUEST_OUTCOME, status="empty") == 2
    snap = tel.snapshot()
    assert snap["request_outcome_total{status=empty}"] == 2
    assert snap["request_outcome_total{status=unavailable}"] == 1


def test_telemetry_rejects_high_cardinality_label_values():
    tel = Telemetry()
    # Exception text, user IDs, queries, and paths must never become labels.
    with pytest.raises(ValueError):
        tel.increment(METRIC_REQUEST_OUTCOME, detail="Traceback: boom happened")
    with pytest.raises(ValueError):
        tel.increment(METRIC_REQUEST_OUTCOME, user="bkoye2009@gmail.com")
    with pytest.raises(ValueError):
        tel.increment(METRIC_REQUEST_OUTCOME, path="C:/Users/secret/file.pdf")


def test_telemetry_rejects_unsafe_metric_and_label_names():
    tel = Telemetry()
    with pytest.raises(ValueError):
        tel.increment("Bad Metric Name", status="empty")
    with pytest.raises(ValueError):
        tel.increment(METRIC_REQUEST_OUTCOME, **{"Bad Label": "empty"})


def test_record_request_outcome_uses_enum_values():
    tel = Telemetry()
    tel.record_request_outcome(
        OutcomeStatus.UNAVAILABLE, FailureCode.SEARCH_BACKEND_UNAVAILABLE
    )
    snap = tel.snapshot()
    assert (
        snap["request_outcome_total{code=search.backend_unavailable,status=unavailable}"]
        == 1
    )


def test_record_acl_metadata_drop_accumulates():
    tel = Telemetry()
    tel.record_acl_metadata_drop(3)
    tel.record_acl_metadata_drop(0)  # no-op
    assert tel.get(METRIC_ACL_METADATA_DROP) == 3


# ---------------------------------------------------------------------------
# Redis fail policy (item 2): fail open for throttling, fail closed for leases
# ---------------------------------------------------------------------------


class _ScriptRaises:
    """Fake Redis whose rate-limit Lua script raises when invoked."""

    def register_script(self, _script):
        def _run(*args, **kwargs):
            raise RuntimeError("redis down")

        return _run


class _LeaseRaises:
    """Fake Redis whose lease primitives raise."""

    def register_script(self, _script):
        return lambda *a, **k: 0

    def set(self, *args, **kwargs):
        raise RuntimeError("redis down")

    def delete(self, *args, **kwargs):
        raise RuntimeError("redis down")


class _LeaseWorks:
    def register_script(self, _script):
        return lambda *a, **k: 0

    def set(self, *args, **kwargs):
        return True

    def delete(self, *args, **kwargs):
        return 1


def test_query_rate_limit_fails_open_and_is_observable():
    limiter = RedisRateLimiter(_ScriptRaises())

    # Fail open: the request is allowed rather than blocked.
    assert limiter.is_rate_limited("query:user", max_calls=1, window_seconds=60) is False

    # ...but the degradation is observable.
    assert (
        get_telemetry().get(
            METRIC_SERVICE_DEGRADED,
            component=COMPONENT_RATE_LIMITER,
            code=FailureCode.RATE_LIMIT_BACKEND_UNAVAILABLE,
        )
        == 1
    )
    assert get_readiness().get(COMPONENT_RATE_LIMITER) is Readiness.DEGRADED


def test_lease_acquire_fails_closed_when_backend_errors():
    limiter = RedisRateLimiter(_LeaseRaises())

    # Integrity-critical exclusivity must be denied, never granted on error.
    assert limiter.acquire_lease("fra:building", duration_seconds=30) is False
    assert (
        get_telemetry().get(
            METRIC_SERVICE_DEGRADED,
            component=COMPONENT_RESOURCE_LEASE,
            code=FailureCode.RATE_LIMIT_BACKEND_UNAVAILABLE,
        )
        == 1
    )


def test_lease_release_fails_closed_when_backend_errors():
    limiter = RedisRateLimiter(_LeaseRaises())
    assert limiter.release_lease("fra:building") is False


def test_lease_acquire_succeeds_without_degradation():
    limiter = RedisRateLimiter(_LeaseWorks())
    assert limiter.acquire_lease("fra:building", duration_seconds=30) is True
    assert get_telemetry().get(
        METRIC_SERVICE_DEGRADED,
        component=COMPONENT_RESOURCE_LEASE,
        code=FailureCode.RATE_LIMIT_BACKEND_UNAVAILABLE,
    ) == 0


def test_manager_without_redis_publishes_degraded_readiness():
    manager = RateLimiterManager()
    manager.initialise(None)
    assert get_readiness().get(COMPONENT_RATE_LIMITER) is Readiness.DEGRADED
    assert isinstance(manager._backend, InMemoryRateLimiter)


def test_manager_with_working_redis_publishes_ready():
    class _Pingable:
        def ping(self):
            return True

        def register_script(self, _script):
            return lambda *a, **k: 0

    manager = RateLimiterManager()
    manager.initialise(_Pingable())
    assert get_readiness().get(COMPONENT_RATE_LIMITER) is Readiness.READY
    assert isinstance(manager._backend, RedisRateLimiter)


# ---------------------------------------------------------------------------
# Access-context posture (item 6, AUTH-13)
# ---------------------------------------------------------------------------


def test_anonymous_rejected_before_retrieval_when_auth_mandatory():
    failure = validate_access_context(
        authenticated=False,
        tenant_id=None,
        user_roles=(),
        auth_mandatory=True,
    )
    assert isinstance(failure, FailureInfo)
    assert failure.code is FailureCode.ACCESS_CONTEXT_INVALID
    assert failure.retryable is False


def test_anonymous_allowed_when_auth_not_mandatory():
    assert (
        validate_access_context(
            authenticated=False,
            tenant_id=None,
            user_roles=(),
            auth_mandatory=False,
        )
        is None
    )


def test_build_access_filter_deny_all_for_anonymous_when_mandatory():
    access_filter = build_access_filter(
        tenant_id=None,
        user_roles=(),
        authenticated=False,
        auth_mandatory=True,
    )
    assert access_filter == {"tenant_id": {"$eq": DENY_ALL_TENANT_ID}}


def test_build_access_filter_empty_for_anonymous_when_optional():
    access_filter = build_access_filter(
        tenant_id=None,
        user_roles=(),
        authenticated=False,
        auth_mandatory=False,
    )
    assert access_filter == {}


def test_authenticated_missing_tenant_still_rejected_regardless_of_mandatory():
    failure = validate_access_context(
        authenticated=True,
        tenant_id="   ",
        user_roles=("base_view",),
        auth_mandatory=False,
    )
    assert failure is not None
    assert failure.code is FailureCode.ACCESS_CONTEXT_INVALID


def test_auth_is_mandatory_reflects_config(monkeypatch):
    monkeypatch.setattr(query_context, "IS_PRODUCTION", False)
    monkeypatch.setattr(query_context, "REQUIRE_AUTH", False)
    monkeypatch.setattr(query_context, "ALLOW_ANONYMOUS_DEV", True)
    assert auth_is_mandatory() is False

    monkeypatch.setattr(query_context, "REQUIRE_AUTH", True)
    assert auth_is_mandatory() is True

    monkeypatch.setattr(query_context, "REQUIRE_AUTH", False)
    monkeypatch.setattr(query_context, "ALLOW_ANONYMOUS_DEV", False)
    assert auth_is_mandatory() is True


# ---------------------------------------------------------------------------
# ACL conformance and drops (item 5, AUTH-10)
# ---------------------------------------------------------------------------


def _compliant_match(mid: str) -> dict:
    return {
        "id": mid,
        "metadata": {
            "tenant_id": "tenant-a",
            "access_level": "pilot_internal",
            "allowed_roles": ["base_view"],
        },
    }


def _noncompliant_match(mid: str) -> dict:
    return {"id": mid, "metadata": {"tenant_id": "tenant-a"}}


def test_missing_acl_matches_dropped_and_counted_under_active_filter():
    access_filter = {"tenant_id": {"$eq": "tenant-a"}}
    matches = [_compliant_match("1"), _noncompliant_match("2")]

    kept = filter_authorized_matches(matches, access_filter=access_filter)

    assert [m["id"] for m in kept] == ["1"]
    assert get_telemetry().get(METRIC_ACL_METADATA_DROP) == 1


def test_no_acl_drop_recorded_without_active_filter():
    matches = [_noncompliant_match("2")]
    kept = filter_authorized_matches(matches, access_filter=None)
    # Unscoped (dev/anonymous) session keeps legacy vectors and records no drop.
    assert [m["id"] for m in kept] == ["2"]
    assert get_telemetry().get(METRIC_ACL_METADATA_DROP) == 0


def test_measure_acl_conformance_counts_and_threshold():
    records = [
        _compliant_match("1"),
        _compliant_match("2"),
        _noncompliant_match("3"),
    ]
    conformance = measure_acl_conformance(records)
    assert conformance.total == 3
    assert conformance.compliant == 2
    assert conformance.missing == 1
    assert conformance.conformance_ratio == pytest.approx(2 / 3)
    assert conformance.meets_threshold(0.6)
    assert not conformance.meets_threshold(0.9)


def test_measure_acl_conformance_empty_is_fully_conformant():
    conformance = measure_acl_conformance([])
    assert conformance.total == 0
    assert conformance.conformance_ratio == 1.0
    assert conformance.meets_threshold(1.0)


# ---------------------------------------------------------------------------
# QueryManager degradation wiring (items 1, 3)
# ---------------------------------------------------------------------------


def _bare_manager() -> QueryManager:
    """A QueryManager instance without the heavy classifier/handler init."""
    return object.__new__(QueryManager)


def test_building_directory_degraded_for_scoped_query(monkeypatch):
    monkeypatch.setattr(
        "query_core.query_manager.BuildingCacheManager.is_populated",
        staticmethod(lambda: False),
    )
    manager = _bare_manager()
    context = QueryContext(query="issues at Senate House", building_filter="Senate House")

    degraded = manager._record_building_directory_readiness(context)

    assert degraded is True
    assert get_readiness().get(COMPONENT_BUILDING_DIRECTORY) is Readiness.DEGRADED
    assert (
        get_telemetry().get(
            METRIC_FALLBACK_ACTIVATED, component=COMPONENT_BUILDING_DIRECTORY
        )
        == 1
    )


def test_building_directory_ready_when_populated(monkeypatch):
    monkeypatch.setattr(
        "query_core.query_manager.BuildingCacheManager.is_populated",
        staticmethod(lambda: True),
    )
    manager = _bare_manager()
    context = QueryContext(query="issues at Senate House", building_filter="Senate House")

    assert manager._record_building_directory_readiness(context) is False
    assert get_readiness().get(COMPONENT_BUILDING_DIRECTORY) is Readiness.READY


def test_building_directory_unscoped_query_not_warned_but_still_degraded(monkeypatch):
    monkeypatch.setattr(
        "query_core.query_manager.BuildingCacheManager.is_populated",
        staticmethod(lambda: False),
    )
    manager = _bare_manager()
    context = QueryContext(query="how many open jobs are there")

    # No building scope -> no per-query warning, but the component is degraded.
    assert manager._record_building_directory_readiness(context) is False
    assert get_readiness().get(COMPONENT_BUILDING_DIRECTORY) is Readiness.DEGRADED


def test_apply_building_directory_degradation_downgrades_trustworthy_result():
    manager = _bare_manager()
    result = QueryResult(query="q", answer="a", status=OutcomeStatus.SUCCESS)

    manager._apply_building_directory_degradation(result, degraded=True)

    assert result.status is OutcomeStatus.DEGRADED
    assert COMPONENT_BUILDING_DIRECTORY in result.degraded_components


def test_apply_building_directory_degradation_never_upgrades_worse_outcome():
    manager = _bare_manager()
    failure = FailureInfo.from_code(
        FailureCode.SEARCH_BACKEND_UNAVAILABLE, "retrieval"
    )
    result = QueryResult(
        query="q", answer=None, status=OutcomeStatus.UNAVAILABLE, failure=failure
    )

    manager._apply_building_directory_degradation(result, degraded=True)

    # An unavailable result must not be softened to degraded.
    assert result.status is OutcomeStatus.UNAVAILABLE
    assert COMPONENT_BUILDING_DIRECTORY in result.degraded_components


def test_apply_building_directory_degradation_noop_when_healthy():
    manager = _bare_manager()
    result = QueryResult(query="q", answer="a", status=OutcomeStatus.SUCCESS)
    manager._apply_building_directory_degradation(result, degraded=False)
    assert result.status is OutcomeStatus.SUCCESS
    assert result.degraded_components == []


def test_record_outcome_telemetry_captures_status_and_code():
    manager = _bare_manager()
    failure = FailureInfo.from_code(
        FailureCode.SEARCH_BACKEND_UNAVAILABLE, "retrieval"
    )
    result = QueryResult(
        query="q", answer=None, status=OutcomeStatus.UNAVAILABLE, failure=failure
    )

    manager._record_outcome_telemetry(result)

    assert (
        get_telemetry().get(
            METRIC_REQUEST_OUTCOME,
            status=OutcomeStatus.UNAVAILABLE,
            code=FailureCode.SEARCH_BACKEND_UNAVAILABLE,
        )
        == 1
    )


def test_record_intent_classifier_degraded():
    manager = _bare_manager()
    manager._record_intent_classifier_degraded()
    assert get_readiness().get(COMPONENT_INTENT_CLASSIFIER) is Readiness.DEGRADED
    assert (
        get_telemetry().get(
            METRIC_FALLBACK_ACTIVATED, component=COMPONENT_INTENT_CLASSIFIER
        )
        == 1
    )
