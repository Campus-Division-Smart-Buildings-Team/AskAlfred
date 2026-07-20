#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Delegates counting logic to counting_queries.generate_counting_answer
and avoids overlap with maintenance, ranking, or property-condition routing.
"""

import re

from core.outcomes import OutcomeStatus
from query_core.query_context import QueryContext
from query_core.query_result import QueryResult

# First party import
from query_core.query_types import QueryType
from search_core.structured_queries import (
    generate_counting_answer_with_outcome,
    is_counting_query,
    is_maintenance_query,
    is_property_condition_query,
    is_ranking_query,
)
from security.log_sanitiser import sanitise_error

# Local import
from .base_handler import BaseQueryHandler
from .handler_failures import handler_failed_result


class CountingHandler(BaseQueryHandler):
    """Handles pure counting queries (e.g., 'how many buildings…')."""

    def __init__(self):
        super().__init__()
        self.query_type = QueryType.COUNTING
        self.priority = 5

        # Restrictive patterns that *only* identify pure counting intent
        self.patterns = [
            re.compile(r"\bhow\s+many\s+buildings?\b", re.IGNORECASE),
            re.compile(r"\bcount\s+(?:the\s+)?buildings?\b", re.IGNORECASE),
            re.compile(r"\bnumber\s+of\s+buildings?\b", re.IGNORECASE),
            re.compile(r"\blist\s+all\s+buildings?\b", re.IGNORECASE),
        ]

    def can_handle(self, context: QueryContext) -> bool:
        """
        Only handle counting queries that are NOT:
        - maintenance queries
        - ranking queries
        - property condition queries

        Those are routed by their respective handlers.
        """
        q = context.query.strip()

        # Explicitly avoid conflicts with other handlers
        if is_maintenance_query(q):
            return False
        if is_ranking_query(q):
            return False
        if is_property_condition_query(q):
            return False

        # Use both pattern matching and structured_queries detection
        if any(p.search(q.lower()) for p in self.patterns):
            return True

        # Secondary check: allow counting_queries to confirm intent
        return is_counting_query(q)

    def handle(self, context: QueryContext) -> QueryResult:
        """Produce a structured counting answer using counting_queries."""
        self._log_handling(context)
        query_text = context.query.strip()

        try:
            outcome = generate_counting_answer_with_outcome(
                query_text,
                access_filter=context.access_filter,
            )

            answer = outcome.answer
            if not answer and outcome.status is OutcomeStatus.EMPTY:
                answer = (
                    "I couldn't determine what to count in your query. "
                    "Try asking about buildings, document types, or maintenance data."
                )

            return QueryResult(
                query=query_text,
                answer=answer,
                results=[],
                handler_used="CountingHandler",
                query_type=self.query_type.value,
                status=outcome.status,
                failure=outcome.failure,
                degraded_components=outcome.degraded_components,
                source_outcomes=outcome.source_outcomes,
                metadata={
                    "structured_response": True,
                    "status": outcome.status.value,
                },
            )
        except Exception as e:
            self.logger.error(
                "Counting handler error: %s", sanitise_error(e), exc_info=False
            )
            return handler_failed_result(
                query_text,
                "CountingHandler",
                self.query_type.value,
                error_code="counting_handler_error",
            )
