# search_core/search_router.py

from core.alfred_exceptions import RoutingError
from search_core.search_instructions import SearchInstructions

from .maintenance_search import maintenance_search_with_outcome
from .planon_search import planon_search_with_outcome
from .semantic_search import semantic_search_with_outcome

# ------------------------------------------------------------------------------------
# Router
# ------------------------------------------------------------------------------------


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
