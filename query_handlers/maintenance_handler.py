#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Improved MaintenanceHandler.
Handles maintenance-related requests, jobs, categories, and metrics.
Delegates logic to generate_maintenance_answers.generate_maintenance_answer.
"""

# First party import
from core.outcomes import OutcomeStatus
from query_core.query_context import QueryContext
from query_core.query_result import QueryResult
from query_core.query_types import QueryType
from search_core.generate_maintenance_answers import (
    generate_maintenance_answer_with_outcome,
)
from search_core.structured_queries import (
    is_maintenance_query,
    is_property_condition_query,
    is_ranking_query,
)
from security.log_sanitiser import sanitise_error

# Local import
from .base_handler import BaseQueryHandler
from .handler_failures import handler_failed_result


class MaintenanceHandler(BaseQueryHandler):
    """Handles maintenance requests, categories, and maintenance job lookup queries."""

    def __init__(self):
        super().__init__()
        self.query_type = QueryType.MAINTENANCE
        self.priority = 2

    def can_handle(self, context: QueryContext) -> bool:
        """
        MaintenanceHandler should only activate for genuine maintenance questions.
        Avoid overlaps with:
          - ranking queries (e.g., "largest backlog")
          - property condition queries
          - general counting queries
        """

        q = context.query.strip().lower()

        # Exclusions must run first: "rank buildings by maintenance backlog"
        # mentions maintenance but belongs to the ranking handler.
        if is_ranking_query(q):
            return False
        if is_property_condition_query(q):
            return False

        return is_maintenance_query(q)

    def _as_name(self, building):
        if not building:
            return None
        return getattr(building, "name", None) or str(building)

    def handle(self, context: QueryContext) -> QueryResult:
        """Produce structured maintenance information via structured_queries logic."""
        self._log_handling(context)
        query_text = context.query.strip()

        try:
            prev_building = None
            previous_context = getattr(context, "previous_context", None)
            if previous_context:
                prev_building = previous_context.get("building")

            building_override = (
                self._as_name(context.building)
                or context.building_filter
                or prev_building
            )

            outcome = generate_maintenance_answer_with_outcome(
                query_text,
                building_override=building_override,
                access_filter=context.access_filter,
            )

            answer = outcome.answer
            if not answer and outcome.status is OutcomeStatus.EMPTY:
                answer = (
                    "I couldn't identify any maintenance information for your query. "
                    "You can try specifying a building, maintenance category, or job type."
                )

            return QueryResult(
                query=query_text,
                answer=answer,
                results=[],
                handler_used="MaintenanceHandler",
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
                "Maintenance handler error: %s", sanitise_error(e), exc_info=False
            )

            # Metadata may be displayed/persisted by callers, so expose only a
            # stable error code; the detailed text stays in the sanitized logs.
            return handler_failed_result(
                query_text,
                "MaintenanceHandler",
                self.query_type.value,
                error_code="maintenance_handler_error",
            )
