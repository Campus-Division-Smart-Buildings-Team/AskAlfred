# search_core/maintenance_search.py

from search_core.generate_maintenance_answers import (
    generate_maintenance_answer_with_outcome,
)


def maintenance_search_with_outcome(instruction):
    """Return a maintenance answer with per-index retrieval health."""

    return generate_maintenance_answer_with_outcome(
        instruction.query,
        access_filter=getattr(instruction, "access_filter", None),
    )
