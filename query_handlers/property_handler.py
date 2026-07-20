#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Improved PropertyHandler.
Handles queries about building property conditions (Condition A–D, Derelict, etc.).
Delegates logic to counting_queries.generate_property_condition_answer.
"""

import re

from core.outcomes import OutcomeStatus
from query_core.query_context import QueryContext
from query_core.query_result import QueryResult

# First party import
from query_core.query_types import QueryType
from search_core.structured_queries import (
    generate_property_condition_answer_with_outcome,
    is_counting_query,
    is_maintenance_query,
    is_property_condition_query,
    is_ranking_query,
)
from security.log_sanitiser import sanitise_error

# Local import
from .base_handler import BaseQueryHandler
from .handler_failures import handler_failed_result


class PropertyHandler(BaseQueryHandler):
    """Handles building property condition queries."""

    def __init__(self):
        super().__init__()
        self.query_type = QueryType.PROPERTY_CONDITION
        self.priority = 4

        # Expanded and precise patterns for property conditions
        self.patterns = [
            # direct "condition A/B/C/D"
            re.compile(r"\bcondition\s*[a-d]\b", re.IGNORECASE),
            # "in condition A"
            re.compile(r"\b(?:in|is)\s+condition\s+[a-d]\b", re.IGNORECASE),
            # derelict
            re.compile(r"\bderelict\b", re.IGNORECASE),
            # explicit phrase
            re.compile(r"\bproperty\s+condition\b", re.IGNORECASE),
            # properly handle the test case
            re.compile(
                r"\bwhich\s+buildings?\s+(are|is)\s+condition\s+[a-d](?:\b|[?!.,])",
                re.IGNORECASE,
            ),
            # general fallback for "which buildings ... condition A"
            re.compile(
                r"\bwhich\s+buildings?.*?\bcondition\s+[a-d](?:\b|[?!.,])",
                re.IGNORECASE,
            ),
        ]

    def can_handle(self, context: QueryContext) -> bool:
        """
        PropertyHandler should only handle property condition queries.
        Avoid overlaps with:
          - maintenance
          - ranking
          - counting queries
        """

        q = context.query.strip().lower()
        self.logger.info("🔍 Checking PropertyHandler for: %s", q)

        # Avoid overlap with other handlers
        if is_maintenance_query(q):
            self.logger.info("🚫 Skipping because query is maintenance-related.")
            return False
        if is_ranking_query(q):
            self.logger.info("🚫 Skipping because query is ranking-related.")
            return False
        if is_counting_query(q):
            self.logger.info("🚫 Skipping because query is counting-related.")
            return False

        # Direct regex or keyword check
        if any(p.search(q) for p in self.patterns):
            self.logger.info("✅ PropertyHandler matched by regex pattern.")
            return True

        # Keyword fallback
        if "derelict" in q or "condition" in q or is_property_condition_query(q):
            self.logger.info("✅ PropertyHandler matched by keyword fallback.")
            return True

        self.logger.info("❌ PropertyHandler did not match.")
        return False

    def handle(self, context: QueryContext) -> QueryResult:
        """Generate the property condition answer."""
        self._log_handling(context)
        query_text = context.query.strip()

        try:
            outcome = generate_property_condition_answer_with_outcome(
                query_text,
                access_filter=context.access_filter,
            )

            answer = outcome.answer
            if not answer and outcome.status is OutcomeStatus.EMPTY:
                answer = (
                    "I couldn't interpret a valid property condition in your query. "
                    "Try specifying Condition A, B, C, D, or 'derelict'."
                )

            return QueryResult(
                query=query_text,
                answer=answer,
                results=[],
                handler_used="PropertyHandler",
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
                "Property handler error: %s", sanitise_error(e), exc_info=False
            )
            return handler_failed_result(
                query_text,
                "PropertyHandler",
                self.query_type.value,
                error_code="property_handler_error",
            )
