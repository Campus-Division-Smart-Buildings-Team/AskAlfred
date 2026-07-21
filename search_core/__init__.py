# search_core/__init__.py

"""
Unified search core package.

Provides (all on the structured per-source outcome contract):
- semantic_search_with_outcome()   vector semantic retrieval
- planon_search_with_outcome()     structured property/condition/ranking logic
- maintenance_search_with_outcome() structured maintenance lookups
- execute_with_outcome()           unified router for SearchInstructions
"""

from .maintenance_search import maintenance_search_with_outcome
from .planon_search import planon_search_with_outcome
from .retrieval_outcomes import (
    SemanticOutcome,
    StructuredAnswerOutcome,
    aggregate_source_outcomes,
)

# Router for SearchInstructions
from .search_router import execute_with_outcome

# Utilities (optional re-export)
from .search_utils import (
    apply_building_boost,
    apply_doc_type_boost,
    deduplicate_results,
    get_effective_score,
    search_one_index,
)
from .semantic_search import semantic_search_with_outcome

__all__ = [
    "semantic_search_with_outcome",
    "SemanticOutcome",
    "StructuredAnswerOutcome",
    "aggregate_source_outcomes",
    "planon_search_with_outcome",
    "maintenance_search_with_outcome",
    "execute_with_outcome",
    "search_one_index",
    "deduplicate_results",
    "apply_doc_type_boost",
    "apply_building_boost",
    "get_effective_score",
]
