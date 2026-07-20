"""Required/optional classification for federated retrieval sources.

This is the executable counterpart to
``plan/dependency_and_source_classification.md``. A source must be classified
here before it is added to ``TARGET_INDEXES``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class SourceRequirement(str, Enum):
    """Whether an aggregate operation requires a retrieval source."""

    REQUIRED = "required"
    OPTIONAL = "optional"


@dataclass(frozen=True)
class SourceClassification:
    """Stable configuration for one retrieval source."""

    source: str
    requirement: SourceRequirement


RETRIEVAL_SOURCE_CLASSIFICATIONS: dict[str, SourceClassification] = {
    "testacl": SourceClassification(
        source="testacl",
        requirement=SourceRequirement.REQUIRED,
    ),
}


def validate_target_index_classification(target_indexes: Iterable[str]) -> None:
    """Require an exact classification for every configured target index."""

    configured = set(target_indexes)
    classified = set(RETRIEVAL_SOURCE_CLASSIFICATIONS)
    missing = configured - classified
    unexpected = classified - configured
    if missing or unexpected:
        raise ValueError(
            "Retrieval-source classification does not match TARGET_INDEXES "
            f"(missing={sorted(missing)!r}, unexpected={sorted(unexpected)!r})"
        )


__all__ = [
    "RETRIEVAL_SOURCE_CLASSIFICATIONS",
    "SourceClassification",
    "SourceRequirement",
    "validate_target_index_classification",
]
