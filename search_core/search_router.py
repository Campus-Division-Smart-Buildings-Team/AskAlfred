# search_core/search_router.py

from typing import Any, Optional, Union

from core.alfred_exceptions import RoutingError, SearchError
from search_core.search_instructions import SearchInstructions

from .maintenance_search import maintenance_search, maintenance_search_with_outcome
from .planon_search import planon_search, planon_search_with_outcome
from .semantic_search import semantic_search, semantic_search_with_outcome

# ------------------------------------------------------------------------------------
# Return type contracts (must match actual backend implementations)
# ------------------------------------------------------------------------------------

# semantic_search returns:
#   (results, answer, publication_info, score_too_low)
SemanticReturn = tuple[list[dict[str, Any]], str, str, bool]

# planon_search returns:
#   (results, answer, publication_info)
# The publication_info is always "" in the current implementation.
PlanonReturn = tuple[list[dict[str, Any]], Optional[str], str]

# maintenance_search returns:
#   (results, answer)
MaintenanceReturn = tuple[list[dict[str, Any]], Optional[str]]

# Unified router return type:
ReturnUnion = Union[SemanticReturn, PlanonReturn, MaintenanceReturn]


def normalise_execute_result(
    raw: ReturnUnion | tuple,
) -> tuple[list[dict[str, Any]], str, str, bool]:
    """Normalise a legacy router result and reject contract drift."""

    if len(raw) == 4:
        results, answer, publication_info, score_too_low = raw
        return results, answer or "", publication_info or "", bool(score_too_low)
    if len(raw) == 3:
        results, answer, publication_info = raw
        return results, answer or "", publication_info or "", False
    if len(raw) == 2:
        results, answer = raw
        return results, answer or "", "", False
    raise SearchError(
        f"search_core.execute returned an unexpected arity: {len(raw)}"
    )


# ------------------------------------------------------------------------------------
# Router
# ------------------------------------------------------------------------------------


def execute(instr: SearchInstructions) -> ReturnUnion:
    """
    Unified router that delegates to the appropriate search backend based on
    the SearchInstructions.type field.

    Expected return shapes:
      - type == "semantic":    (List[Dict], str, str, bool)
      - type == "planon":      (List[Dict], str, str)
      - type == "maintenance": (List[Dict], str)
    """
    itype = getattr(instr, "type", None)

    # Semantic vector search
    if itype == "semantic":
        return semantic_search(
            query=instr.query,
            top_k=instr.top_k,
            building_filter=getattr(instr, "building", None),
            access_filter=getattr(instr, "access_filter", None),
        )

    # Planon structured search (property/condition/ranking)
    if itype == "planon":
        # planon_search returns: (results, answer, publication_info)
        return planon_search(instr)

    # Maintenance structured search
    if itype == "maintenance":
        # maintenance_search returns: (results, answer)
        return maintenance_search(instr)

    raise RoutingError(f"Unknown search instruction type: {itype}")


def execute_with_outcome(instr: SearchInstructions):
    """Route a search instruction without dropping its structured outcome."""

    itype = getattr(instr, "type", None)
    if itype == "semantic":
        return semantic_search_with_outcome(
            query=instr.query,
            top_k=instr.top_k,
            building_filter=getattr(instr, "building", None),
            access_filter=getattr(instr, "access_filter", None),
        )
    if itype == "planon":
        return planon_search_with_outcome(instr)
    if itype == "maintenance":
        return maintenance_search_with_outcome(instr)
    raise RoutingError(f"Unknown search instruction type: {itype}")
