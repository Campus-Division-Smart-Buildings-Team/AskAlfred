#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Default semantic search handler.
Handles all remaining queries not claimed by other handlers.
"""

import logging
import time
from typing import Optional

from query_core.query_context import QueryContext
from query_core.query_result import QueryResult
from query_core.query_types import QueryType
from search_core.search_instructions import SearchInstructions
from search_core.search_router import execute_with_outcome
from search_core.semantic_search import semantic_search_with_outcome
from security.log_sanitiser import sanitise_error

from .base_handler import BaseQueryHandler
from .handler_failures import handler_failed_result


class SemanticSearchHandler(BaseQueryHandler):
    """Fallback handler performing federated semantic search."""

    def __init__(self):
        super().__init__()
        self.query_type = QueryType.SEMANTIC_SEARCH
        self.priority = 99
        self.min_query_length = 2  # min words required
        self.min_char_length = 4  # min characters to avoid noise
        self.timeout_seconds = 12  # guardrail for heavy searches

    def can_handle(self, context: QueryContext) -> bool:
        """
        This is the fallback handler,
        but we apply a soft check to avoid meaningless semantic lookups.
        """
        q = context.query.strip()

        # Prevent semantic search on extremely short fragments
        if len(q) < self.min_char_length:
            return True

        if len(q.split()) < self.min_query_length:
            return True

        return True

    def handle(self, context: QueryContext) -> QueryResult:
        """Run federated semantic search with safety guards."""
        instructions: Optional[SearchInstructions] = context.get_from_cache(
            "search_instructions"
        )

        # If a handler provided explicit search instructions, run them
        if instructions:
            return self._execute_instructions(context, instructions)

        ml_intent = getattr(context, "predicted_intent", None)
        ml_conf = getattr(context, "ml_intent_confidence", 0.0)
        if ml_intent:
            # light-touch hint; you could thread this into your search router or boosts
            context.add_to_cache(
                "ml_intent_hint",
                {
                    "intent": (
                        ml_intent.value
                        if hasattr(ml_intent, "value")
                        else str(ml_intent)
                    ),
                    "confidence": ml_conf,
                },
            )

        self._log_handling(context)
        query_text = context.query.strip()

        # Soft handling of very short queries
        if len(query_text) < self.min_char_length:
            return QueryResult(
                query=query_text,
                answer="Could you tell me a bit more? I need a little more detail to search properly.",
                results=[],
                handler_used="SemanticSearchHandler",
                query_type=self.query_type.value,
                metadata={"short_query": True},
            )

        if len(query_text.split()) < self.min_query_length:
            return QueryResult(
                query=query_text,
                answer="Just a few more words would help me understand what you're looking for.",
                results=[],
                handler_used="SemanticSearchHandler",
                query_type=self.query_type.value,
                metadata={"short_query": True},
            )

        # Run federated search with timeout
        start = time.time()

        try:
            outcome = semantic_search_with_outcome(
                query_text,
                context.top_k,
                building_filter=context.building_filter,
                access_filter=context.access_filter,
            )

            elapsed = round(time.time() - start, 3)

            # Retrieval health drives the structured status: a backend outage is
            # `unavailable`, incomplete coverage is `partial`, an answer-generation
            # failure is `partial` with results retained, healthy zero matches is
            # `empty`. The UI branches on status rather than on answer text.
            return QueryResult(
                query=query_text,
                answer=outcome.answer or None,
                results=outcome.results,
                publication_date_info=outcome.publication_info,
                score_too_low=outcome.score_too_low,
                handler_used="SemanticSearchHandler",
                query_type=self.query_type.value,
                status=outcome.status,
                failure=outcome.failure,
                degraded_components=outcome.degraded_components,
                source_outcomes=outcome.source_outcomes,
                metadata={
                    "num_results": len(outcome.results),
                    "elapsed_seconds": elapsed,
                    "score_too_low": outcome.score_too_low,
                    "status": outcome.status.value,
                    "building_filter": context.building_filter,
                    "access_control_applied": bool(context.access_filter),
                },
            )

        except Exception as e:
            logging.error(
                "Semantic search failure: %s", sanitise_error(e), exc_info=False
            )
            return handler_failed_result(
                query_text,
                "SemanticSearchHandler",
                self.query_type.value,
                error_code="semantic_search_error",
            )

    def _execute_instructions(
        self, context: QueryContext, instr: SearchInstructions
    ) -> QueryResult:
        """Execute a structured search instruction from another handler.

        Semantic instructions run through the structured outcome path so a
        backend outage or answer-generation failure carries its status; planon
        and maintenance instructions preserve the same status contract, mapping
        a total structured outage to ``unavailable``.
        """
        start = time.time()

        try:
            outcome = execute_with_outcome(instr)
            elapsed = round(time.time() - start, 3)
            results = getattr(outcome, "results", [])

            return QueryResult(
                query=context.query,
                answer=outcome.answer or None,
                results=results,
                publication_date_info=getattr(outcome, "publication_info", None),
                score_too_low=getattr(outcome, "score_too_low", None),
                handler_used="SemanticSearchHandler",
                query_type=self.query_type.value,
                status=outcome.status,
                failure=outcome.failure,
                degraded_components=outcome.degraded_components,
                source_outcomes=outcome.source_outcomes,
                metadata={
                    "instruction_type": instr.type,
                    "elapsed_seconds": elapsed,
                    "building_filter": instr.building,
                    "num_results": len(results),
                    "status": outcome.status.value,
                },
            )
        except Exception as e:
            logging.error(
                "Search instruction failed: %s", sanitise_error(e), exc_info=False
            )
            return handler_failed_result(
                context.query,
                "SemanticSearchHandler",
                self.query_type.value,
                error_code="search_instruction_error",
            )
