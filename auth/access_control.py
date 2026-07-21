from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from auth.auth_context import AuthContext
from config import OPERATOR_ROLES
from core.telemetry import get_telemetry

REQUIRED_ACL_FIELDS = ("tenant_id", "access_level", "allowed_roles")


def is_operator(auth_context: AuthContext) -> bool:
    """Return True only for an authenticated user holding an operator app role.

    Fails closed: an anonymous session, a missing/empty ``roles`` claim, or a
    claim that shares no value with ``OPERATOR_ROLES`` is not an operator.
    Comparison is case-sensitive to match the Entra ID app-role ``value``.
    """
    if not auth_context.authenticated:
        return False
    operator_roles = {role.strip() for role in OPERATOR_ROLES if role.strip()}
    if not operator_roles:
        return False
    return any(str(role).strip() in operator_roles for role in auth_context.roles)


def combine_pinecone_filters(
    left: Optional[dict[str, Any]],
    right: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Combine two Pinecone filters with a logical AND."""
    filters = [filter_dict for filter_dict in (left, right) if filter_dict]
    if not filters:
        return None
    if len(filters) == 1:
        return filters[0]

    clauses: list[dict[str, Any]] = []
    for filter_dict in filters:
        if (
            isinstance(filter_dict, dict)
            and set(filter_dict.keys()) == {"$and"}
            and isinstance(filter_dict["$and"], list)
        ):
            clauses.extend(filter_dict["$and"])
        else:
            clauses.append(filter_dict)
    return {"$and": clauses}


def filter_authorized_structured_matches(
    matches: list[dict[str, Any]],
    access_filter: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Fail closed on missing ACL metadata and drop matches outside access scope."""
    authorised_matches: list[dict[str, Any]] = []
    missing_acl_drops = 0
    for match in matches:
        metadata = match.get("metadata", {}) or {}
        # Only enforce the ACL envelope when an access filter is in play;
        # legacy vectors without ACL metadata remain visible to unscoped
        # (anonymous/dev) sessions until they are re-ingested.
        if access_filter and not has_required_acl_metadata(metadata):
            # Track non-compliant vectors so ACL conformance is measurable and
            # "no authorised results" stays distinct from a silent ACL drop
            # (AUTH-10). The vector's identity is never used as a metric label.
            missing_acl_drops += 1
            continue
        if access_filter and not metadata_matches_filter(metadata, access_filter):
            continue
        authorised_matches.append(match)
    if missing_acl_drops:
        get_telemetry().record_acl_metadata_drop(missing_acl_drops)
    return authorised_matches


@dataclass(frozen=True)
class AclConformance:
    """Measured ACL-envelope conformance across a set of vector metadata records."""

    total: int
    compliant: int
    missing: int

    @property
    def conformance_ratio(self) -> float:
        """Fraction of records carrying the full ACL envelope (1.0 when empty)."""
        if self.total == 0:
            return 1.0
        return self.compliant / self.total

    def meets_threshold(self, threshold: float) -> bool:
        """True when conformance is at or above ``threshold`` (0.0-1.0)."""
        return self.conformance_ratio >= threshold


def measure_acl_conformance(
    records: list[dict[str, Any]],
) -> AclConformance:
    """Measure ACL-envelope conformance for a batch of metadata dicts.

    Each record may be a raw metadata dict or a match wrapping ``metadata``.
    This is the measurement primitive behind the Phase 3 exit criterion "ACL
    conformance can be measured"; it identifies vectors that would be dropped
    by :func:`filter_authorized_structured_matches` so they can be re-ingested
    or quarantined.
    """
    total = 0
    compliant = 0
    for record in records:
        metadata = record.get("metadata") if "metadata" in record else record
        metadata = metadata or {}
        total += 1
        if has_required_acl_metadata(metadata):
            compliant += 1
    return AclConformance(total=total, compliant=compliant, missing=total - compliant)


def filter_authorized_matches(
    matches: list[dict[str, Any]],
    access_filter: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Generic alias for fail-closed match filtering across retrieval paths."""
    return filter_authorized_structured_matches(matches, access_filter=access_filter)


def has_required_acl_metadata(metadata: dict[str, Any]) -> bool:
    """Return True only when the minimum structured ACL envelope is present."""
    for field in REQUIRED_ACL_FIELDS:
        value = metadata.get(field)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if field == "allowed_roles":
            if not isinstance(value, (list, tuple, set)):
                return False
            if not any(str(role).strip() for role in value):
                return False
    return True


def apply_acl_defaults(
    metadata: dict[str, Any],
    *,
    tenant_id: Optional[str] = None,
    access_level: Optional[str] = None,
    allowed_roles: Optional[list[str] | tuple[str, ...]] = None,
) -> dict[str, Any]:
    """Stamp default ACL fields into metadata when they are absent."""
    if tenant_id and not metadata.get("tenant_id"):
        metadata["tenant_id"] = tenant_id
    if access_level and not metadata.get("access_level"):
        metadata["access_level"] = access_level
    if allowed_roles is not None and not metadata.get("allowed_roles"):
        metadata["allowed_roles"] = [
            str(role).strip() for role in allowed_roles if str(role).strip()
        ]
    return metadata


def metadata_matches_filter(
    metadata: dict[str, Any], filter_dict: dict[str, Any]
) -> bool:
    """Evaluate a small Pinecone-style metadata filter against a record."""
    if not filter_dict:
        return True

    if "$and" in filter_dict:
        clauses = filter_dict.get("$and") or []
        return all(
            metadata_matches_filter(metadata, clause)
            for clause in clauses
            if isinstance(clause, dict)
        )

    if "$or" in filter_dict:
        clauses = filter_dict.get("$or") or []
        return any(
            metadata_matches_filter(metadata, clause)
            for clause in clauses
            if isinstance(clause, dict)
        )

    for field, condition in filter_dict.items():
        if field.startswith("$"):
            return False
        if not _value_matches_condition(metadata.get(field), condition):
            return False
    return True


def _value_matches_condition(value: Any, condition: Any) -> bool:
    if isinstance(condition, dict):
        if "$eq" in condition:
            expected = condition["$eq"]
            return value == expected
        if "$in" in condition:
            options = condition["$in"] or []
            if isinstance(value, (list, tuple, set)):
                return any(item in options for item in value)
            return value in options
        return False
    return value == condition
