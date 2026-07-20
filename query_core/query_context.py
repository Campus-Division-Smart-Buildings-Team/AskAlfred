#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
QueryContext - Holds all query-related information during processing.

It is passed through:
    • Preprocessors (BuildingExtractor, BusinessTermExtractor, etc.)
    • Handler routing (QueryManager)
    • Handlers (Counting, Ranking, Maintenance, etc.)

Preprocessors enrich this object; handlers consume it.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from core.failure_codes import FailureCode
from core.outcomes import FailureInfo
from query_core.query_types import QueryType

DENY_ALL_TENANT_ID = "__deny_access__"

ACCESS_CONTROL_COMPONENT = "access_control"


def validate_access_context(
    *,
    authenticated: bool,
    tenant_id: Optional[str],
    user_roles: tuple[str, ...],
) -> Optional[FailureInfo]:
    """Check whether a session has enough access context to run retrieval.

    Returns ``None`` when retrieval may proceed, or a :class:`FailureInfo`
    describing a stable access-context rejection that must be surfaced
    *before* retrieval. This prevents an authenticated session with no usable
    tenant or app roles from producing misleading or over-broad retrieval.

    Anonymous/dev sessions are intentionally not rejected here: their posture
    (AUTH-13) is a deployment decision handled separately. Production always
    requires authentication, so this exception is limited to the explicit
    development-only guest posture.
    """
    if not authenticated:
        return None

    if not tenant_id or not str(tenant_id).strip():
        return FailureInfo.from_code(
            FailureCode.ACCESS_CONTEXT_INVALID,
            ACCESS_CONTROL_COMPONENT,
        )

    roles = tuple(str(role).strip() for role in user_roles if str(role).strip())
    if not roles:
        return FailureInfo.from_code(
            FailureCode.ACCESS_ROLE_CONTEXT_INVALID,
            ACCESS_CONTROL_COMPONENT,
        )

    return None


def build_access_filter(
    *,
    tenant_id: Optional[str],
    user_roles: tuple[str, ...],
    authenticated: bool,
) -> dict[str, Any]:
    """
    Build the first-pass retrieval access filter from the current auth context.

    Authenticated users are constrained to their tenant and at least one
    asserted app role. Missing tenant or role context receives a defence-in-
    depth deny-all filter even if a caller accidentally skips
    :func:`validate_access_context`. Anonymous sessions remain deliberately
    unfiltered only for the explicitly enabled development guest posture.
    """
    if not authenticated:
        return {}

    roles = [str(role).strip() for role in user_roles if str(role).strip()]
    if not tenant_id or not str(tenant_id).strip() or not roles:
        return {"tenant_id": {"$eq": DENY_ALL_TENANT_ID}}

    access_filter: dict[str, Any] = {"tenant_id": {"$eq": str(tenant_id)}}
    return {
        "$and": [
            access_filter,
            {"allowed_roles": {"$in": roles}},
        ]
    }


@dataclass
class QueryContext:
    """
    Represents all relevant state for a single user query.

    Attributes filled by QueryManager:
        query (str): Raw user query.
        created_at (float): Timestamp when context was created.
        top_k (int): Number of results requested by the user (e.g. for semantic search).
        building_filter (str | None): Explicit building filter passed by the user.
        cache (dict): Internal scratchpad for preprocessors & handlers.

    Attributes enriched by preprocessors:
        building (str | None)
        business_terms (list)
        document_type (str | None)
        complexity (str)
        corrected_query (str | None)

    Attributes enriched by handlers:
        (optional) anything added via context.add_to_cache()
    """

    # Required user fields
    query: str
    top_k: int = 10
    building_filter: Optional[str] = None
    history: Optional[list[dict[str, Any]]] = None
    rolling_summary: Optional[str] = None
    user_id: str = "anonymous"
    user_name: Optional[str] = None
    tenant_id: Optional[str] = None
    user_roles: tuple[str, ...] = field(default_factory=tuple)
    authenticated: bool = False
    auth_source: str = "anonymous"
    # None means "not yet built" (QueryManager builds it from the auth context);
    # an explicit {} means "deliberately unfiltered" and is preserved.
    access_filter: Optional[dict[str, Any]] = None

    # Preprocessor-enriched attributes
    building: Optional[str] = None
    buildings: list[str] = field(default_factory=list)
    business_terms: list[dict[str, Any]] = field(default_factory=list)
    document_type: Optional[str] = None
    complexity: Optional[str] = None
    corrected_query: Optional[str] = None

    # Internal scratchpad
    cache: dict[str, Any] = field(default_factory=dict)

    # Metadata
    created_at: float = field(default_factory=time.time)

    # ML intent (router enrichment)
    predicted_intent: Optional[QueryType] = None
    ml_intent_confidence: float = 0.0
    routing_notes: list[str] = field(default_factory=list)
    # Previous query memory (restored from SessionManager)
    previous_context: Optional[dict[str, Any]] = None
    previous_intent: Optional[str] = None
    previous_intent_confidence: Optional[float] = None

    # ----------------------------------------------------------------------
    # Context helper methods
    # ----------------------------------------------------------------------

    def add_to_cache(self, key: str, value: Any) -> None:
        """Store arbitrary metadata used by preprocessors or handlers."""
        self.cache[key] = value

    def get_from_cache(self, key: str, default: Any = None) -> Any:
        """Retrieve cached information."""
        return self.cache.get(key, default)

    def update_query(self, new_query: str) -> None:
        """
        Used primarily by SpellChecker or normalisation preprocessors.
        Records previous queries for debugging.
        """
        self.add_to_cache("previous_query", self.query)
        self.query = new_query
        self.corrected_query = new_query

    def has_business_term(self, term_type: Optional[str] = None) -> bool:
        """
        Returns True if any business term was extracted,
        optionally filtered by term type (e.g., document_type="FRA").
        """
        if not self.business_terms:
            return False

        if term_type is None:
            return True

        return any(t.get("type") == term_type for t in self.business_terms)

    def __repr__(self) -> str:
        return (
            f"QueryContext("
            f"query={self.query!r}, user_id={self.user_id!r}, building={self.building!r}, "
            f"document_type={self.document_type!r}, complexity={self.complexity!r}, "
            f"prev_intent={self.previous_intent!r})"
        )
