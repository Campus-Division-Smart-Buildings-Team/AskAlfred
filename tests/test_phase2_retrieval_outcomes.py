"""Phase 2 tests: query and retrieval truthfulness.

Covers the four Phase 2 exit criteria:

- A simulated all-index outage never produces no-results copy (``unavailable``).
- A simulated one-index outage with other results produces a partial warning.
- Genuine healthy zero matches produces only the empty state.
- An answer-generation outage still shows retrieved results (``partial``).

Plus the supporting contract: typed per-source outcomes, source aggregation,
structured-search unavailability, cache exclusion, and the SEARCH-12 router
contract guard.
"""

from __future__ import annotations

import importlib

import pytest

from core.alfred_exceptions import (
    AnswerGenerationError,
    StructuredSearchUnavailable,
)
from core.failure_codes import FailureCode
from core.outcomes import FailureInfo, OutcomeStatus, SourceOutcome
from query_core.query_manager import _CACHEABLE_STATUSES
from search_core import search_utils
from search_core.retrieval_outcomes import (
    RETRIEVAL_COMPONENT,
    aggregate_source_outcomes,
    raise_if_backend_unavailable,
    structured_answer_outcome,
)
from search_core.semantic_search import semantic_search_with_outcome

# The ``search_core`` package re-exports ``semantic_search`` as a function, which
# shadows the submodule attribute; fetch the real module for monkeypatching.
semantic_module = importlib.import_module("search_core.semantic_search")
structured_module = importlib.import_module("search_core.structured_queries")


# ---------------------------------------------------------------------------
# Aggregation rules (plan section B)
# ---------------------------------------------------------------------------


def _healthy(source: str, count: int = 1) -> SourceOutcome:
    status = OutcomeStatus.SUCCESS if count else OutcomeStatus.EMPTY
    return SourceOutcome(source=source, status=status, result_count=count)


def _unavailable(source: str, code: FailureCode) -> SourceOutcome:
    return SourceOutcome(
        source=source,
        status=OutcomeStatus.UNAVAILABLE,
        failure=FailureInfo.from_code(code, RETRIEVAL_COMPONENT),
    )


def test_aggregate_empty_list_is_success():
    status, failure = aggregate_source_outcomes([])
    assert status is OutcomeStatus.SUCCESS
    assert failure is None


def test_aggregate_all_healthy_is_success():
    status, failure = aggregate_source_outcomes(
        [_healthy("testacl", 0), _healthy("other", 3)]
    )
    assert status is OutcomeStatus.SUCCESS
    assert failure is None


def test_aggregate_single_required_outage_is_unavailable_with_specific_code():
    status, failure = aggregate_source_outcomes(
        [_unavailable("testacl", FailureCode.SEARCH_INDEX_UNAVAILABLE)]
    )
    assert status is OutcomeStatus.UNAVAILABLE
    assert failure is not None
    # The specific per-source cause is preserved for a single-source outage.
    assert failure.code is FailureCode.SEARCH_INDEX_UNAVAILABLE


def test_aggregate_all_required_fail_is_backend_unavailable():
    status, failure = aggregate_source_outcomes(
        [
            _unavailable("idx-a", FailureCode.SEARCH_INDEX_UNAVAILABLE),
            _unavailable("idx-b", FailureCode.SEARCH_EMBEDDING_UNAVAILABLE),
        ]
    )
    assert status is OutcomeStatus.UNAVAILABLE
    assert failure is not None
    assert failure.code is FailureCode.SEARCH_BACKEND_UNAVAILABLE


def test_aggregate_mixed_success_and_failure_is_partial():
    status, failure = aggregate_source_outcomes(
        [_healthy("idx-a", 5), _unavailable("idx-b", FailureCode.SEARCH_INDEX_UNAVAILABLE)]
    )
    assert status is OutcomeStatus.PARTIAL
    assert failure is not None
    assert failure.code is FailureCode.SEARCH_SOURCE_PARTIAL


def test_aggregate_partial_source_is_partial_not_unavailable():
    partial_source = SourceOutcome(
        source="testacl",
        status=OutcomeStatus.PARTIAL,
        result_count=2,
        failure=FailureInfo.from_code(
            FailureCode.SEARCH_NAMESPACE_UNAVAILABLE, RETRIEVAL_COMPONENT
        ),
    )
    status, failure = aggregate_source_outcomes([partial_source])
    assert status is OutcomeStatus.PARTIAL


def test_raise_if_backend_unavailable_raises_only_on_total_outage():
    with pytest.raises(StructuredSearchUnavailable):
        raise_if_backend_unavailable(
            [_unavailable("testacl", FailureCode.STRUCTURED_SEARCH_UNAVAILABLE)]
        )

    # A healthy source must not raise.
    raise_if_backend_unavailable([_healthy("testacl", 0)])


def test_structured_answer_preserves_partial_source_health():
    outcome = structured_answer_outcome(
        "A usable structured answer",
        [
            _healthy("idx-a", 2),
            _unavailable("idx-b", FailureCode.STRUCTURED_SEARCH_UNAVAILABLE),
        ],
    )

    assert outcome.status is OutcomeStatus.PARTIAL
    assert outcome.answer == "A usable structured answer"
    assert outcome.failure is not None
    assert outcome.source_outcomes[1].status is OutcomeStatus.UNAVAILABLE
    assert "structured_retrieval" in outcome.degraded_components


def test_structured_generator_carries_partial_outcome_to_handler_boundary(monkeypatch):
    def fake_rank(*args, _source_outcomes=None, **kwargs):
        _source_outcomes.extend(
            [
                _healthy("idx-a", 1),
                _unavailable(
                    "idx-b", FailureCode.STRUCTURED_SEARCH_UNAVAILABLE
                ),
            ]
        )
        return {
            "total_buildings": 1,
            "results": [
                {
                    "rank": 1,
                    "building_name": "Senate House",
                    "value": 100.0,
                    "metadata": {},
                }
            ],
        }

    monkeypatch.setattr(structured_module, "rank_buildings_by_area", fake_rank)

    outcome = structured_module.generate_ranking_answer_with_outcome(
        "Rank buildings by gross area"
    )

    assert outcome.status is OutcomeStatus.PARTIAL
    assert "Senate House" in outcome.answer
    assert len(outcome.source_outcomes) == 2


@pytest.mark.parametrize(
    ("status", "cacheable"),
    [
        (OutcomeStatus.SUCCESS, True),
        (OutcomeStatus.EMPTY, True),
        (OutcomeStatus.LOW_CONFIDENCE, True),
        (OutcomeStatus.PARTIAL, False),
        (OutcomeStatus.DEGRADED, False),
        (OutcomeStatus.UNAVAILABLE, False),
        (OutcomeStatus.FAILED, False),
    ],
)
def test_only_trustworthy_terminal_query_outcomes_are_cacheable(status, cacheable):
    assert (status in _CACHEABLE_STATUSES) is cacheable


# ---------------------------------------------------------------------------
# search_one_index_with_outcome: per-source classification
# ---------------------------------------------------------------------------


def _patch_index_plumbing(monkeypatch, *, namespaces):
    monkeypatch.setattr(search_utils, "open_index", lambda idx_name: object())
    monkeypatch.setattr(
        search_utils, "get_index_config", lambda idx_name: {"model": "m"}
    )
    monkeypatch.setattr(
        search_utils, "_namespaces_to_search", lambda idx, idx_name: namespaces
    )
    monkeypatch.setattr(search_utils, "embed_texts", lambda texts, model: [[0.0, 0.1]])
    monkeypatch.setattr(search_utils, "filter_authorized_matches", lambda hits, access_filter: hits)


def test_search_one_index_open_failure_is_unavailable(monkeypatch):
    def boom(idx_name):
        raise RuntimeError("index open failed")

    monkeypatch.setattr(search_utils, "open_index", boom)

    hits, outcome = search_utils.search_one_index_with_outcome("testacl", "q")

    assert hits == []
    assert outcome.status is OutcomeStatus.UNAVAILABLE
    assert outcome.failure.code is FailureCode.SEARCH_INDEX_UNAVAILABLE


def test_search_one_index_embedding_failure_is_unavailable(monkeypatch):
    _patch_index_plumbing(monkeypatch, namespaces=[None])

    def boom(texts, model):
        raise RuntimeError("embedding failed")

    monkeypatch.setattr(search_utils, "embed_texts", boom)

    # No query_vector provided -> embedding runs and fails.
    hits, outcome = search_utils.search_one_index_with_outcome("testacl", "q")

    assert hits == []
    assert outcome.status is OutcomeStatus.UNAVAILABLE
    assert outcome.failure.code is FailureCode.SEARCH_EMBEDDING_UNAVAILABLE


def test_search_one_index_non_retryable_embedding_failure_is_failed(monkeypatch):
    _patch_index_plumbing(monkeypatch, namespaces=[None])

    class AuthenticationError(Exception):
        """Provider-style non-retryable authentication failure."""

    monkeypatch.setattr(
        search_utils,
        "embed_texts",
        lambda texts, model: (_ for _ in ()).throw(AuthenticationError()),
    )

    hits, outcome = search_utils.search_one_index_with_outcome("testacl", "q")

    assert hits == []
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.failure.code is FailureCode.SEARCH_EMBEDDING_FAILED
    assert outcome.failure.retryable is False


def test_search_one_index_all_namespaces_fail_is_unavailable(monkeypatch):
    _patch_index_plumbing(monkeypatch, namespaces=[None])

    def boom(*args, **kwargs):
        raise RuntimeError("namespace query failed")

    monkeypatch.setattr(search_utils, "vector_query", boom)

    hits, outcome = search_utils.search_one_index_with_outcome(
        "testacl", "q", query_vector=[0.0, 0.1]
    )

    assert hits == []
    assert outcome.status is OutcomeStatus.UNAVAILABLE
    assert outcome.failure.code is FailureCode.SEARCH_NAMESPACE_UNAVAILABLE


def test_search_one_index_partial_namespace_failure_is_partial(monkeypatch):
    _patch_index_plumbing(monkeypatch, namespaces=[None, "ns2"])
    monkeypatch.setattr(
        search_utils, "normalise_matches", lambda raw: [{"id": "1", "score": 0.9}]
    )

    def vector_query(idx, namespace, query, k, embed_model, metadata_filter=None, query_vector=None):
        if namespace == "ns2":
            raise RuntimeError("ns2 down")
        return {"matches": []}

    monkeypatch.setattr(search_utils, "vector_query", vector_query)

    hits, outcome = search_utils.search_one_index_with_outcome(
        "testacl", "q", query_vector=[0.0, 0.1]
    )

    assert len(hits) == 1
    assert outcome.status is OutcomeStatus.PARTIAL
    assert outcome.failure.code is FailureCode.SEARCH_NAMESPACE_UNAVAILABLE


def test_search_one_index_healthy_hits_is_success(monkeypatch):
    _patch_index_plumbing(monkeypatch, namespaces=[None])
    monkeypatch.setattr(
        search_utils, "normalise_matches", lambda raw: [{"id": "1", "score": 0.9}]
    )
    monkeypatch.setattr(
        search_utils,
        "vector_query",
        lambda *a, **k: {"matches": []},
    )

    hits, outcome = search_utils.search_one_index_with_outcome(
        "testacl", "q", query_vector=[0.0, 0.1]
    )

    assert len(hits) == 1
    assert outcome.status is OutcomeStatus.SUCCESS
    assert outcome.result_count == 1
    assert outcome.failure is None


def test_search_one_index_healthy_no_hits_is_empty(monkeypatch):
    _patch_index_plumbing(monkeypatch, namespaces=[None])
    monkeypatch.setattr(search_utils, "normalise_matches", lambda raw: [])
    monkeypatch.setattr(search_utils, "vector_query", lambda *a, **k: {"matches": []})

    hits, outcome = search_utils.search_one_index_with_outcome(
        "testacl", "q", query_vector=[0.0, 0.1]
    )

    assert hits == []
    assert outcome.status is OutcomeStatus.EMPTY
    assert outcome.failure is None


# ---------------------------------------------------------------------------
# semantic_search_with_outcome: the four Phase 2 exit criteria
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_semantic(monkeypatch):
    """Neutralise building/term/answer plumbing so tests drive retrieval only."""

    monkeypatch.setattr(semantic_module, "extract_building_from_query", lambda q: None)
    monkeypatch.setattr(semantic_module, "resolve_building_name_fuzzy", lambda b: None)
    monkeypatch.setattr(
        semantic_module.BusinessTermMapper,
        "enhance_query_with_terms",
        staticmethod(lambda q: (q, {})),
    )
    monkeypatch.setattr(
        semantic_module, "get_index_config", lambda idx_name: {"model": "m"}
    )
    monkeypatch.setattr(semantic_module, "embed_texts", lambda texts, model: [[0.0, 0.1]])

    def _answer(query, top_hits, building, term_context):
        return "Generated answer [S1]", "pub info", top_hits

    monkeypatch.setattr(semantic_module, "_generate_semantic_answer", _answer)
    return monkeypatch


def _set_indexes(monkeypatch, names):
    monkeypatch.setattr(semantic_module, "TARGET_INDEXES", names)


def test_all_index_outage_is_unavailable_never_empty(stub_semantic, monkeypatch):
    _set_indexes(monkeypatch, ["testacl"])

    def all_down(idx, query, k, building_filter=None, access_filter=None, query_vector=None):
        return [], SourceOutcome(
            source=idx,
            status=OutcomeStatus.UNAVAILABLE,
            failure=FailureInfo.from_code(
                FailureCode.SEARCH_INDEX_UNAVAILABLE, RETRIEVAL_COMPONENT
            ),
        )

    monkeypatch.setattr(semantic_module, "search_one_index_with_outcome", all_down)

    outcome = semantic_search_with_outcome("anything", top_k=5)

    assert outcome.status is OutcomeStatus.UNAVAILABLE
    assert outcome.results == []
    # Never a "couldn't find" / empty answer for a dependency outage.
    assert outcome.answer == ""
    assert outcome.failure is not None


def test_one_index_outage_with_results_is_partial(stub_semantic, monkeypatch):
    _set_indexes(monkeypatch, ["idx-a", "idx-b"])

    def mixed(idx, query, k, building_filter=None, access_filter=None, query_vector=None):
        if idx == "idx-a":
            hit = {"id": "1", "score": 0.9}
            return [hit], SourceOutcome(source=idx, status=OutcomeStatus.SUCCESS, result_count=1)
        return [], SourceOutcome(
            source=idx,
            status=OutcomeStatus.UNAVAILABLE,
            failure=FailureInfo.from_code(
                FailureCode.SEARCH_INDEX_UNAVAILABLE, RETRIEVAL_COMPONENT
            ),
        )

    monkeypatch.setattr(semantic_module, "search_one_index_with_outcome", mixed)

    outcome = semantic_search_with_outcome("anything", top_k=5)

    assert outcome.status is OutcomeStatus.PARTIAL
    assert outcome.results  # successful index's results are retained
    assert RETRIEVAL_COMPONENT in outcome.degraded_components


def test_healthy_zero_matches_is_empty(stub_semantic, monkeypatch):
    _set_indexes(monkeypatch, ["testacl"])

    def healthy_empty(idx, query, k, building_filter=None, access_filter=None, query_vector=None):
        return [], SourceOutcome(source=idx, status=OutcomeStatus.EMPTY)

    monkeypatch.setattr(semantic_module, "search_one_index_with_outcome", healthy_empty)

    outcome = semantic_search_with_outcome("anything", top_k=5)

    assert outcome.status is OutcomeStatus.EMPTY
    assert outcome.results == []
    assert outcome.failure is None


def test_answer_generation_outage_is_partial_with_results(stub_semantic, monkeypatch):
    _set_indexes(monkeypatch, ["testacl"])

    def healthy_hits(idx, query, k, building_filter=None, access_filter=None, query_vector=None):
        hit = {"id": "1", "score": 0.9}
        return [hit], SourceOutcome(source=idx, status=OutcomeStatus.SUCCESS, result_count=1)

    monkeypatch.setattr(semantic_module, "search_one_index_with_outcome", healthy_hits)

    def boom(query, top_hits, building, term_context):
        raise AnswerGenerationError("llm down")

    monkeypatch.setattr(semantic_module, "_generate_semantic_answer", boom)

    outcome = semantic_search_with_outcome("anything", top_k=5)

    assert outcome.status is OutcomeStatus.PARTIAL
    assert outcome.results  # retrieved results are still shown
    assert outcome.answer == ""
    assert outcome.failure.code is FailureCode.ANSWER_GENERATION_UNAVAILABLE
    assert "answer_generation" in outcome.degraded_components


def test_empty_answer_generation_response_is_partial_with_results(
    stub_semantic, monkeypatch
):
    _set_indexes(monkeypatch, ["testacl"])
    hit = {"id": "1", "score": 0.9}
    monkeypatch.setattr(
        semantic_module,
        "search_one_index_with_outcome",
        lambda *args, **kwargs: (
            [hit],
            SourceOutcome(
                source="testacl", status=OutcomeStatus.SUCCESS, result_count=1
            ),
        ),
    )
    monkeypatch.setattr(
        semantic_module,
        "_generate_semantic_answer",
        lambda *args, **kwargs: ("", "", [hit]),
    )

    outcome = semantic_search_with_outcome("anything", top_k=5)

    assert outcome.status is OutcomeStatus.PARTIAL
    assert outcome.results == [hit]
    assert outcome.answer == ""
    assert outcome.failure.code is FailureCode.ANSWER_GENERATION_UNAVAILABLE


def test_healthy_success_returns_answer(stub_semantic, monkeypatch):
    _set_indexes(monkeypatch, ["testacl"])

    def healthy_hits(idx, query, k, building_filter=None, access_filter=None, query_vector=None):
        hit = {"id": "1", "score": 0.9}
        return [hit], SourceOutcome(source=idx, status=OutcomeStatus.SUCCESS, result_count=1)

    monkeypatch.setattr(semantic_module, "search_one_index_with_outcome", healthy_hits)

    outcome = semantic_search_with_outcome("anything", top_k=5)

    assert outcome.status is OutcomeStatus.SUCCESS
    assert outcome.answer == "Generated answer [S1]"
    assert outcome.results


def test_low_score_hits_is_low_confidence(stub_semantic, monkeypatch):
    _set_indexes(monkeypatch, ["testacl"])

    def low_score(idx, query, k, building_filter=None, access_filter=None, query_vector=None):
        hit = {"id": "1", "score": 0.01}
        return [hit], SourceOutcome(source=idx, status=OutcomeStatus.SUCCESS, result_count=1)

    monkeypatch.setattr(semantic_module, "search_one_index_with_outcome", low_score)

    outcome = semantic_search_with_outcome("anything", top_k=5)

    assert outcome.status is OutcomeStatus.LOW_CONFIDENCE
    assert outcome.score_too_low is True
    assert outcome.results


