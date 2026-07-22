"""Phase 5 operational-rollout tests.

Covers the rollout machinery and the legacy-path removal:

* feature flags and the presenter kill-switch;
* the query-side Prometheus metrics exporter;
* the outcome-metric alert rules and their generated artifact;
* the production-gated fault-injection harness and representative seams;
* the outcome-rate baseline comparison;
* removal of the ``QueryResult.success`` boolean and the legacy tuple router.
"""

from __future__ import annotations

import importlib
import logging
import threading
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.failure_codes import FailureCode
from core.outcomes import OutcomeStatus, SourceOutcome, is_successful
from core.telemetry import ReadinessRegistry, Telemetry
from query_core.query_result import QueryResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_fault_injector():
    from core.fault_injection import get_fault_injector
    from core.telemetry import get_telemetry

    get_fault_injector().clear()
    get_telemetry().reset()
    yield
    get_fault_injector().clear()
    get_telemetry().reset()


def _telemetry_with(*outcomes) -> Telemetry:
    telemetry = Telemetry()
    for status, code in outcomes:
        telemetry.record_request_outcome(status, code)
    return telemetry


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------


def test_feature_flags_default_on(monkeypatch):
    from config import feature_flags

    monkeypatch.delenv("USE_QUERY_MANAGER", raising=False)
    monkeypatch.delenv("USE_STRUCTURED_PRESENTER", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    assert feature_flags.use_query_manager() is True
    assert feature_flags.use_structured_presenter() is True
    assert feature_flags.is_production() is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", False), ("0", False), ("off", False), ("true", True), ("", True)],
)
def test_feature_flag_env_override(monkeypatch, value, expected):
    from config import feature_flags

    monkeypatch.setenv("USE_STRUCTURED_PRESENTER", value)
    assert feature_flags.use_structured_presenter() is expected


def test_is_production_reads_environment_live(monkeypatch):
    from config import feature_flags

    monkeypatch.setenv("ENVIRONMENT", "production")
    assert feature_flags.is_production() is True


# ---------------------------------------------------------------------------
# Presenter kill-switch
# ---------------------------------------------------------------------------


def test_kill_switch_bypasses_structured_presenter(monkeypatch):
    from ui import error_presenter

    monkeypatch.setenv("USE_STRUCTURED_PRESENTER", "false")
    presented = error_presenter.safe_present_outcome(OutcomeStatus.EMPTY)

    # The rich EMPTY copy is replaced by the static fallback.
    assert presented.severity == "error"
    assert presented.reference is not None
    assert presented.message == error_presenter._FALLBACK_MESSAGE


def test_safe_present_outcome_never_raises(monkeypatch):
    from ui import error_presenter

    monkeypatch.setenv("USE_STRUCTURED_PRESENTER", "true")

    def boom(*_args, **_kwargs):
        raise RuntimeError("presenter bug")

    monkeypatch.setattr(error_presenter, "present_outcome", boom)

    presented = error_presenter.safe_present_outcome(OutcomeStatus.FAILED)
    assert presented.message == error_presenter._FALLBACK_MESSAGE
    assert presented.reference is not None


def test_safe_present_query_failure_never_raises(monkeypatch):
    from ui import error_presenter

    class Boom:
        @property
        def status(self):
            raise RuntimeError("bad result object")

        failure = None

    presented = error_presenter.safe_present_query_failure(Boom())
    assert presented.message == error_presenter._FALLBACK_MESSAGE


# ---------------------------------------------------------------------------
# Service metrics exporter
# ---------------------------------------------------------------------------


def test_service_metrics_render_counters_and_readiness():
    from core.service_metrics import render_service_metrics

    telemetry = _telemetry_with(
        (OutcomeStatus.SUCCESS, None),
        (OutcomeStatus.UNAVAILABLE, FailureCode.SEARCH_BACKEND_UNAVAILABLE),
    )
    readiness = ReadinessRegistry()
    readiness.mark_unavailable("retrieval", FailureCode.SEARCH_BACKEND_UNAVAILABLE)

    text = render_service_metrics(
        telemetry, readiness, export_timestamp_seconds=1_700_000_000
    )

    assert "# TYPE askalfred_request_outcome_total counter" in text
    assert 'askalfred_request_outcome_total{status="success"} 1' in text
    assert "# TYPE askalfred_component_readiness gauge" in text
    assert (
        'askalfred_component_readiness{component="retrieval",'
        'readiness="unavailable",code="search.backend_unavailable"} 1' in text
    )
    assert "askalfred_metrics_export_timestamp_seconds 1700000000.000000" in text


def test_service_metrics_render_successful_and_failed_auth_outcomes():
    from core.service_metrics import render_service_metrics

    telemetry = Telemetry()
    telemetry.record_auth_outcome(OutcomeStatus.SUCCESS)
    telemetry.record_auth_outcome(
        OutcomeStatus.FAILED,
        FailureCode.AUTH_PROVIDER_RESPONSE_INVALID,
    )

    text = render_service_metrics(
        telemetry,
        ReadinessRegistry(),
        export_timestamp_seconds=1_700_000_000,
    )

    assert "# TYPE askalfred_auth_outcome_total counter" in text
    assert 'askalfred_auth_outcome_total{status="success"} 1' in text
    assert (
        'askalfred_auth_outcome_total{code="auth.provider_response_invalid",'
        'status="failed"} 1' in text
    )


def test_service_metrics_empty_still_emits_export_timestamp():
    from core.service_metrics import render_service_metrics

    text = render_service_metrics(
        Telemetry(),
        ReadinessRegistry(),
        export_timestamp_seconds=1_700_000_000,
    )
    assert "askalfred_request_outcome_total" not in text
    assert "askalfred_component_readiness" not in text
    assert "askalfred_metrics_export_timestamp_seconds 1700000000.000000" in text


def test_service_metrics_write_is_atomic(tmp_path):
    from core.service_metrics import write_service_metrics

    telemetry = _telemetry_with((OutcomeStatus.SUCCESS, None))
    out = tmp_path / "nested" / "service.prom"
    write_service_metrics(str(out), telemetry, ReadinessRegistry())

    assert out.exists()
    assert "askalfred_request_outcome_total" in out.read_text(encoding="utf-8")
    assert not list(out.parent.glob(".service.prom.*.tmp"))


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def test_alerts_fire_for_critical_and_unavailable():
    from core.alerts import evaluate_alerts

    telemetry = _telemetry_with(
        (OutcomeStatus.CRITICAL_INCONSISTENT, FailureCode.FRA_CRITICAL_INCONSISTENT)
    )
    readiness = ReadinessRegistry()
    readiness.mark_unavailable("retrieval")

    names = {alert.name for alert in evaluate_alerts(telemetry, readiness)}
    assert "AskAlfredCriticalInconsistentRequest" in names
    assert "AskAlfredComponentUnavailable" in names


def test_alerts_quiet_when_healthy():
    from core.alerts import evaluate_alerts

    telemetry = _telemetry_with((OutcomeStatus.SUCCESS, None))
    assert evaluate_alerts(telemetry, ReadinessRegistry()) == []


def test_elevated_error_rate_needs_volume_and_threshold():
    from core.alerts import ERROR_RATE_MIN_VOLUME, evaluate_alerts

    # Below the minimum volume: no alert even at 100% failure.
    low_volume = _telemetry_with((OutcomeStatus.FAILED, None))
    assert all(
        alert.name != "AskAlfredElevatedErrorRate"
        for alert in evaluate_alerts(low_volume, ReadinessRegistry())
    )

    # Enough volume and >20% failed: alert fires.
    telemetry = Telemetry()
    for _ in range(ERROR_RATE_MIN_VOLUME):
        telemetry.record_request_outcome(OutcomeStatus.SUCCESS)
    for _ in range(ERROR_RATE_MIN_VOLUME):
        telemetry.record_request_outcome(OutcomeStatus.FAILED)
    names = {a.name for a in evaluate_alerts(telemetry, ReadinessRegistry())}
    assert "AskAlfredElevatedErrorRate" in names


def test_alert_rules_artifact_matches_generated():
    import yaml

    from scripts.gen_alert_rules import OUTPUT_PATH, build_artifact

    on_disk = OUTPUT_PATH.read_text(encoding="utf-8")
    assert on_disk == build_artifact(), (
        "ops/askalfred_alerts.yml is stale; run scripts/gen_alert_rules.py"
    )

    doc = yaml.safe_load(on_disk)
    assert doc["groups"][0]["name"] == "askalfred_outcome_alerts"
    assert doc["groups"][0]["rules"], "no rules rendered"


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------


def test_fault_injection_arm_and_fire():
    from core.fault_injection import FaultPoint, get_fault_injector, maybe_fail

    injector = get_fault_injector()
    injector.arm(FaultPoint.PINECONE_QUERY, ValueError, count=1)

    with pytest.raises(ValueError):
        maybe_fail(FaultPoint.PINECONE_QUERY)

    # Budget exhausted -> subsequent calls are no-ops.
    maybe_fail(FaultPoint.PINECONE_QUERY)
    assert not injector.is_armed(FaultPoint.PINECONE_QUERY)


def test_fault_injection_refuses_and_noops_in_production(monkeypatch):
    from core.fault_injection import (
        FaultInjectionDisabled,
        FaultPoint,
        get_fault_injector,
        maybe_fail,
    )

    injector = get_fault_injector()
    # Arm while non-prod, then switch to production: the fault must not fire.
    injector.arm(FaultPoint.REDIS)
    monkeypatch.setenv("ENVIRONMENT", "production")

    maybe_fail(FaultPoint.REDIS)  # no-op in production

    with pytest.raises(FaultInjectionDisabled):
        injector.arm(FaultPoint.REDIS)


def test_configure_from_env_arms_named_points(monkeypatch):
    from core.fault_injection import FaultPoint, configure_from_env, get_fault_injector

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("FAULT_INJECTION", "redis, pinecone_query , bogus")
    armed = configure_from_env()

    assert set(armed) == {"redis", "pinecone_query"}
    assert get_fault_injector().is_armed(FaultPoint.REDIS)


def test_fault_injection_seam_yields_typed_retrieval_outcome():
    from core.fault_injection import FaultPoint, get_fault_injector
    from search_core.search_utils import search_one_index_with_outcome

    get_fault_injector().arm(FaultPoint.PINECONE_INDEX_OPEN, RuntimeError)

    hits, outcome = search_one_index_with_outcome("any-index", "any query")

    # The injected index-open fault flows through the existing typed handling
    # instead of a silent empty result.
    assert hits == []
    assert outcome.status is OutcomeStatus.UNAVAILABLE
    assert outcome.failure is not None
    assert outcome.failure.code is FailureCode.SEARCH_INDEX_UNAVAILABLE


def test_fault_injection_redis_seam(monkeypatch):
    from core.clients import ClientManager
    from core.fault_injection import FaultPoint, get_fault_injector

    get_fault_injector().arm(FaultPoint.REDIS, RuntimeError)
    with pytest.raises(RuntimeError):
        ClientManager.get_redis()


def test_fault_injection_embedding_seam_has_typed_retryable_outcome(monkeypatch):
    from core.fault_injection import FaultPoint, get_fault_injector
    from search_core import search_utils

    monkeypatch.setattr(search_utils, "open_index", lambda _name: object())
    monkeypatch.setattr(
        search_utils, "get_index_config", lambda _name: {"model": "test-model"}
    )
    monkeypatch.setattr(
        search_utils, "_namespaces_to_search", lambda _idx, _name: [None]
    )
    get_fault_injector().arm(FaultPoint.OPENAI_EMBEDDING, TimeoutError, count=1)

    hits, outcome = search_utils.search_one_index_with_outcome("index", "query")

    assert hits == []
    assert outcome.status is OutcomeStatus.UNAVAILABLE
    assert outcome.failure.code is FailureCode.SEARCH_EMBEDDING_UNAVAILABLE
    assert outcome.failure.retryable is True


def test_fault_injection_query_seam_is_unavailable_not_empty(monkeypatch):
    from core.fault_injection import FaultPoint, get_fault_injector
    from search_core import search_utils

    monkeypatch.setattr(search_utils, "open_index", lambda _name: object())
    monkeypatch.setattr(
        search_utils, "get_index_config", lambda _name: {"model": "test-model"}
    )
    monkeypatch.setattr(
        search_utils, "_namespaces_to_search", lambda _idx, _name: [None]
    )
    get_fault_injector().arm(FaultPoint.PINECONE_QUERY, TimeoutError, count=1)

    hits, outcome = search_utils.search_one_index_with_outcome(
        "index", "query", query_vector=[0.0]
    )

    assert hits == []
    assert outcome.status is OutcomeStatus.UNAVAILABLE
    assert outcome.failure.code is FailureCode.SEARCH_NAMESPACE_UNAVAILABLE
    assert outcome.failure.retryable is True


def test_fault_injection_answer_seam_retains_results_as_partial(monkeypatch):
    from core.fault_injection import FaultPoint, get_fault_injector
    from search_core import semantic_search

    monkeypatch.setattr(semantic_search, "TARGET_INDEXES", ["index"])
    monkeypatch.setattr(
        semantic_search, "get_index_config", lambda _name: {"model": "test-model"}
    )
    monkeypatch.setattr(semantic_search, "embed_texts", lambda *_args: [[0.0]])
    monkeypatch.setattr(semantic_search, "extract_building_from_query", lambda _q: None)
    monkeypatch.setattr(semantic_search, "resolve_building_name_fuzzy", lambda _b: None)
    monkeypatch.setattr(
        semantic_search.BusinessTermMapper,
        "enhance_query_with_terms",
        lambda query: (query, None),
    )
    hit = {"id": "doc:1", "score": 1.0, "metadata": {}}
    monkeypatch.setattr(
        semantic_search,
        "search_one_index_with_outcome",
        lambda *_args, **_kwargs: (
            [hit],
            SourceOutcome(source="index", status=OutcomeStatus.SUCCESS, result_count=1),
        ),
    )
    monkeypatch.setattr(semantic_search, "deduplicate_results", lambda values: values)
    monkeypatch.setattr(
        semantic_search, "apply_occupancy_capacity_boost", lambda values, _q: values
    )
    monkeypatch.setattr(
        semantic_search, "get_effective_score", lambda value: value["score"]
    )
    get_fault_injector().arm(FaultPoint.OPENAI_ANSWER, TimeoutError, count=1)

    outcome = semantic_search.semantic_search_with_outcome("query", 5)

    assert outcome.results == [hit]
    assert outcome.status is OutcomeStatus.PARTIAL
    assert outcome.failure.code is FailureCode.ANSWER_GENERATION_UNAVAILABLE
    assert outcome.failure.retryable is True


def test_fault_injection_auth_callback_has_terminal_code_and_telemetry(monkeypatch):
    from auth import auth_manager
    from core.fault_injection import FaultPoint, get_fault_injector
    from core.telemetry import METRIC_AUTH_OUTCOME, get_telemetry

    fake_streamlit = SimpleNamespace(
        session_state={auth_manager.AUTH_FLOW_SESSION_KEY: {"auth_uri": "https://test"}}
    )
    monkeypatch.setattr(auth_manager, "st", fake_streamlit)
    monkeypatch.setattr(
        auth_manager,
        "_get_query_params",
        lambda: {"code": "code", "state": "state"},
    )
    monkeypatch.setattr(auth_manager, "_clear_auth_query_params", lambda: None)
    get_fault_injector().arm(FaultPoint.AUTH_CALLBACK, TimeoutError, count=1)

    assert auth_manager._try_complete_authentication() is None
    failure = fake_streamlit.session_state[auth_manager.AUTH_FAILURE_SESSION_KEY]
    assert failure["code"] == FailureCode.AUTH_PROVIDER_UNAVAILABLE.value
    assert failure["retryable"] is True
    assert (
        get_telemetry().get(
            METRIC_AUTH_OUTCOME,
            status=OutcomeStatus.UNAVAILABLE,
            code=FailureCode.AUTH_PROVIDER_UNAVAILABLE,
        )
        == 1
    )


def test_fault_injection_registry_write_seam_is_not_nominal_success():
    from core.fault_injection import FaultPoint, get_fault_injector
    from ingest.transaction import _record_ingested_files

    ctx = SimpleNamespace(config=SimpleNamespace(dry_run=False))
    get_fault_injector().arm(FaultPoint.REGISTRY_WRITE, RuntimeError, count=1)

    with pytest.raises(RuntimeError, match="registry_write"):
        _record_ingested_files(ctx, [{"id": "file:1"}], status="success")


def test_fault_injection_queue_drain_seam_has_terminal_report():
    from core.fault_injection import FaultPoint, get_fault_injector
    from ingest.batch_ingest import _teardown_worker
    from ingest.transaction import ThreadSafeStats

    ctx = SimpleNamespace(
        config=SimpleNamespace(
            upsert_join_timeout_seconds=0.01,
            upsert_join_poll_seconds=0.001,
        ),
        stats=ThreadSafeStats(),
        logger=logging.getLogger("fault-queue-drain"),
    )
    worker = Mock(name="worker")
    worker.name = "worker"
    worker.is_alive.return_value = False
    queue = Queue()
    get_fault_injector().arm(FaultPoint.QUEUE_DRAIN, TimeoutError, count=1)

    report = _teardown_worker(
        ctx,
        upsert_stop_event=threading.Event(),
        upsert_queue=queue,
        upsert_threads=[worker],
    )

    assert report.queue_timed_out is True
    assert ctx.stats.get_stats()["worker_queue_timeout_total"] == 1


def test_fault_injection_fra_rollback_seam_is_critical_and_observable():
    from core.alfred_exceptions import CriticalInconsistentError
    from core.fault_injection import FaultPoint, get_fault_injector
    from core.telemetry import METRIC_INGEST_INTEGRITY, get_telemetry
    from ingest.transaction import FraTransaction, ThreadSafeStats
    from interfaces import (
        FraJournalState,
        InMemoryFraTransactionJournal,
        new_fra_journal_record,
    )

    journal = InMemoryFraTransactionJournal()
    record = new_fra_journal_record(
        tx_id="tx-fault",
        buildings=["Test Building"],
        requests=[("Test Building", "2026-01-01")],
        vector_ids=["risk:1"],
    )
    journal.begin(record)
    journal.append_superseded("tx-fault", ["old-risk"])
    ctx = SimpleNamespace(
        upsert_stop_event=None,
        fra_journal=journal,
        stats=ThreadSafeStats(),
        logger=logging.getLogger("fault-fra-rollback"),
        event_sink=SimpleNamespace(emit_event=lambda _event: None),
    )
    txn = FraTransaction(ctx, [])
    txn._tx_id = "tx-fault"
    txn._supersede_requests = [("Test Building", "2026-01-01")]
    get_fault_injector().arm(FaultPoint.FRA_ROLLBACK, TimeoutError, count=1)

    with pytest.raises(CriticalInconsistentError):
        txn.rollback("injected_failure")

    assert journal.get("tx-fault").state is FraJournalState.CRITICAL_INCONSISTENT
    assert (
        get_telemetry().get(
            METRIC_INGEST_INTEGRITY,
            event="rollback",
            state="critical_inconsistent",
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Outcome-rate baseline comparison
# ---------------------------------------------------------------------------


def test_outcome_counts_sums_across_codes():
    from core.outcome_rates import outcome_counts

    snapshot = {
        "request_outcome_total{status=success}": 5,
        "request_outcome_total{code=search.backend_unavailable,status=unavailable}": 3,
        "request_outcome_total{code=other,status=unavailable}": 2,
        "source_outcome_total{component=retrieval,status=partial}": 9,
    }
    counts = outcome_counts(snapshot)
    assert counts == {"success": 5, "unavailable": 5}


def test_compare_to_baseline_flags_rise():
    from core.outcome_rates import compare_to_baseline

    baseline = {"success": 90, "unavailable": 10}
    current = {"success": 50, "unavailable": 50}
    regressions = compare_to_baseline(current, baseline)
    statuses = {r.status for r in regressions}
    assert "unavailable" in statuses


def test_compare_to_baseline_skips_low_volume():
    from core.outcome_rates import compare_to_baseline

    baseline = {"success": 100}
    current = {"unavailable": 3}  # below default min volume
    assert compare_to_baseline(current, baseline) == []


# ---------------------------------------------------------------------------
# Legacy removal
# ---------------------------------------------------------------------------


def test_query_result_no_longer_accepts_success_kwarg():
    with pytest.raises(TypeError):
        QueryResult(query="q", answer=None, success=False)


def test_is_successful_helper():
    assert is_successful(OutcomeStatus.PARTIAL) is True
    assert is_successful(OutcomeStatus.FAILED) is False
    assert is_successful("empty") is True


def test_query_result_has_no_success_attribute():
    result = QueryResult(query="q", answer="a")
    assert not hasattr(result, "success")
    assert result.to_dict()["successful"] is True


def test_legacy_tuple_router_paths_removed():
    router = importlib.import_module("search_core.search_router")
    assert not hasattr(router, "execute")
    assert not hasattr(router, "normalise_execute_result")
    assert hasattr(router, "execute_with_outcome")

    semantic = importlib.import_module("search_core.semantic_search")
    assert not hasattr(semantic, "semantic_search")
    assert hasattr(semantic, "semantic_search_with_outcome")

    outcomes = importlib.import_module("search_core.retrieval_outcomes")
    assert not hasattr(outcomes.SemanticOutcome, "as_legacy_tuple")
