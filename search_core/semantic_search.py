# search_core/semantic_search.py

import logging
from typing import Optional

from building.utils import (
    extract_building_from_query,
    group_results_by_building,
    resolve_building_name_fuzzy,
)
from config import MIN_SCORE_THRESHOLD, TARGET_INDEXES, get_index_config
from core.alfred_exceptions import AnswerGenerationError
from core.failure_codes import FailureCode
from core.outcomes import FailureInfo, OutcomeStatus, SourceOutcome
from core.pinecone_utils import embed_texts
from domain.business_terms import BusinessTermMapper
from search_core.generate_semantic_answer import (
    enhanced_answer_with_source_date,
    generate_building_focused_answer,
)
from search_core.retrieval_outcomes import (
    RETRIEVAL_COMPONENT,
    SemanticOutcome,
    aggregate_source_outcomes,
    embedding_failure_outcome,
)
from search_core.search_utils import (
    apply_building_boost,
    apply_doc_type_boost,
    apply_occupancy_capacity_boost,
    deduplicate_results,
    get_effective_score,
    search_one_index_with_outcome,
)
from security.log_sanitiser import sanitise_error

ANSWER_GENERATION_COMPONENT = "answer_generation"

# Retrieval statuses that mean no trustworthy results should be surfaced.
_RETRIEVAL_FAILED_STATUSES = frozenset(
    {OutcomeStatus.UNAVAILABLE, OutcomeStatus.FAILED}
)


def _generate_semantic_answer(
    query: str,
    top_hits: list[dict],
    building: Optional[str],
    term_context: Optional[dict],
) -> tuple[str, str, list[dict]]:
    """Answer-generation stage (separate from retrieval).

    Returns ``(answer, publication_info, ordered_hits)``. Raises on failure so
    the caller can mark the turn ``partial`` and still show direct results,
    rather than encoding an error sentence as a nominal answer.
    """
    building_groups = group_results_by_building(top_hits)

    if building and building_groups.get(building):
        answer, pub_info, cited_results = generate_building_focused_answer(
            query, top_hits[0], top_hits, building, building_groups, term_context
        )
        # Reorder so [SN] citation tags resolve against the returned list:
        # cited sources first (in S-number order), remaining hits after.
        cited_ids = {id(result) for result in cited_results}
        ordered_hits = cited_results + [
            result for result in top_hits if id(result) not in cited_ids
        ]
        return answer, pub_info, ordered_hits

    answer, pub_info = enhanced_answer_with_source_date(
        query, top_hits[0], top_hits, term_context, target_building=building
    )
    return answer, pub_info, top_hits


def semantic_search_with_outcome(
    query: str,
    top_k: int,
    building_filter: Optional[str] = None,
    access_filter: Optional[dict] = None,
) -> SemanticOutcome:
    """Federated semantic search returning a structured :class:`SemanticOutcome`.

    Retrieval and answer generation are tracked as separate stages so a backend
    outage yields ``unavailable``/``partial`` (never a false ``empty``), and an
    answer-generation outage yields ``partial`` with the retrieved results
    retained.
    """
    logging.debug(
        "[semantic_search] running: q=%s, k=%s, building=%s",
        query,
        top_k,
        building_filter,
    )

    # Extract building (or use preset)
    raw_building = building_filter or extract_building_from_query(query)
    building = resolve_building_name_fuzzy(raw_building)

    # Enhance business terms (FRA -> fire risk assessment)
    enhanced_query, term_context = BusinessTermMapper.enhance_query_with_terms(query)

    # Optional document-type boost
    doc_type_filter = None
    if term_context:
        first_term = list(term_context.values())[0]
        doc_type_filter = first_term.get("document_type")

    # Embed once per model up front: both stages and every index reuse the
    # same query vector instead of paying an embedding API call per pass. The
    # embedding call may raise; the caller converts that into an explicit
    # per-source `unavailable` outcome (never a silent empty result).
    vectors_by_model: dict[str, list[float]] = {}
    failed_models: dict[str, Exception] = {}

    def _vector_for(
        idx_name: str,
    ) -> tuple[Optional[list[float]], Exception | None]:
        model = get_index_config(idx_name)["model"]
        if model in vectors_by_model:
            return vectors_by_model[model], None
        if model in failed_models:
            return None, failed_models[model]
        try:
            vector = embed_texts([enhanced_query], model)[0]
        except Exception as error:  # pylint: disable=broad-except
            failed_models[model] = error
            return None, error
        vectors_by_model[model] = vector
        return vector, None

    def _search_all_indexes(
        active_building: Optional[str],
    ) -> tuple[list[dict], list[SourceOutcome]]:
        hits: list[dict] = []
        outcomes: list[SourceOutcome] = []
        for idx in TARGET_INDEXES:
            try:
                vector, embedding_error = _vector_for(idx)
            except Exception as error:  # pylint: disable=broad-except
                embedding_error = error
                vector = None
            if vector is None and embedding_error is not None:
                logging.warning(
                    "Failed to embed query for index '%s': %s",
                    idx,
                    sanitise_error(embedding_error),
                )
                outcomes.append(embedding_failure_outcome(idx, embedding_error))
                continue
            idx_hits, outcome = search_one_index_with_outcome(
                idx,
                enhanced_query,
                top_k * 3,
                building_filter=active_building,
                access_filter=access_filter,
                query_vector=vector,
            )
            hits.extend(idx_hits)
            outcomes.append(outcome)
        return hits, outcomes

    # ===== Stage 1 — building-filtered search =====
    results: list[dict] = []
    source_outcomes: list[SourceOutcome] = []
    used_filter = False

    if building:
        results, source_outcomes = _search_all_indexes(building)
        if results:
            used_filter = True
            results = deduplicate_results(results)

    # ===== Stage 2 — pure semantic fallback =====
    if not results:
        results, source_outcomes = _search_all_indexes(None)
        results = deduplicate_results(results)

    # Aggregate retrieval-source health before interpreting result counts.
    retrieval_status, retrieval_failure = aggregate_source_outcomes(source_outcomes)

    # Apply doc type boost
    if doc_type_filter:
        results = apply_doc_type_boost(results, doc_type_filter)

    # Apply building boost (especially important in stage 2)
    if building:
        results = apply_building_boost(
            results, building, boost_factor=3.0 if not used_filter else 1.5
        )

    results = apply_occupancy_capacity_boost(results, enhanced_query)

    # Sort by boosted or base score
    results.sort(key=get_effective_score, reverse=True)
    top_hits = results[:top_k]

    # A required-source outage must never masquerade as no matching data.
    if retrieval_status in _RETRIEVAL_FAILED_STATUSES:
        return SemanticOutcome(
            results=[],
            answer="",
            publication_info="",
            score_too_low=False,
            status=retrieval_status,
            failure=retrieval_failure,
            source_outcomes=source_outcomes,
        )

    # ===== Interpret result counts once source health is known =====
    answer, pub_info = "", ""
    score_too_low = False
    results_out: list[dict] = []
    degraded_components: list[str] = []
    answer_failure: FailureInfo | None = None
    answer_gen_failed = False

    if not top_hits:
        base_status = OutcomeStatus.EMPTY
    elif get_effective_score(top_hits[0]) < MIN_SCORE_THRESHOLD:
        score_too_low = True
        base_status = OutcomeStatus.LOW_CONFIDENCE
        results_out = top_hits
    else:
        # ===== Answer-generation stage =====
        try:
            answer, pub_info, top_hits = _generate_semantic_answer(
                query, top_hits, building, term_context
            )
            if not isinstance(answer, str) or not answer.strip():
                raise AnswerGenerationError("answer model returned empty content")
            base_status = OutcomeStatus.SUCCESS
            results_out = top_hits
        except Exception as e:  # pylint: disable=broad-except
            logging.error(
                "Answer generation failed: %s", sanitise_error(e), exc_info=False
            )
            answer_gen_failed = True
            answer, pub_info = "", ""
            base_status = OutcomeStatus.SUCCESS
            results_out = top_hits
            answer_failure = FailureInfo.from_code(
                FailureCode.ANSWER_GENERATION_UNAVAILABLE,
                ANSWER_GENERATION_COMPONENT,
                safe_context={"stage": "answer_generation"},
            )
            degraded_components.append(ANSWER_GENERATION_COMPONENT)

    # ===== Resolve the final status =====
    if answer_gen_failed:
        final_status = OutcomeStatus.PARTIAL
        final_failure = answer_failure
    elif retrieval_status is OutcomeStatus.PARTIAL:
        final_status = OutcomeStatus.PARTIAL
        final_failure = retrieval_failure
        degraded_components.append(RETRIEVAL_COMPONENT)
    else:
        final_status = base_status
        final_failure = None

    return SemanticOutcome(
        results=results_out,
        answer=answer,
        publication_info=pub_info,
        score_too_low=score_too_low,
        status=final_status,
        failure=final_failure,
        source_outcomes=source_outcomes,
        degraded_components=degraded_components,
    )


def semantic_search(
    query: str,
    top_k: int,
    building_filter: Optional[str] = None,
    access_filter: Optional[dict] = None,
) -> tuple[list[dict], str, str, bool]:
    """Backward-compatible 4-tuple wrapper around :func:`semantic_search_with_outcome`.

    Returns ``(results, answer, publication_info, score_too_low)``. Callers that
    need the structured status should use :func:`semantic_search_with_outcome`.
    """
    outcome = semantic_search_with_outcome(
        query, top_k, building_filter=building_filter, access_filter=access_filter
    )
    return outcome.as_legacy_tuple()
