# search_core/planon_search.py
from search_core.structured_queries import (
    generate_property_condition_answer_with_outcome,
    generate_ranking_answer_with_outcome,
)


def planon_search_with_outcome(instruction):
    """Return a Planon answer with per-index retrieval health."""

    q = instruction.query.lower()
    access_filter = getattr(instruction, "access_filter", None)
    if "rank" in q or "biggest" in q or "largest" in q:
        return generate_ranking_answer_with_outcome(
            instruction.query,
            access_filter=access_filter,
        )
    return generate_property_condition_answer_with_outcome(
        instruction.query,
        access_filter=access_filter,
    )
