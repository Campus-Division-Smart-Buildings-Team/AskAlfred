from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, cast

from auth.access_control import filter_authorized_structured_matches
from building.utils import (
    BuildingCacheManager,
)
from building.validation import sanitise_building_candidate
from config import TARGET_INDEXES
from core.alfred_exceptions import StructuredSearchUnavailable
from core.failure_codes import FailureCode
from core.outcomes import FailureInfo, OutcomeStatus, SourceOutcome
from core.pinecone_utils import open_index
from domain.maintenance_utils import (
    aggregate_request_metrics,
    aggregate_request_metrics_by_category,
    extract_unresolved_building_phrase,
    filter_maintenance_buildings,
    format_multi_building_metrics,
    is_request_metrics,
    parse_maintenance_query,
)
from search_core.retrieval_outcomes import (
    STRUCTURED_RETRIEVAL_COMPONENT,
    StructuredAnswerOutcome,
    raise_if_backend_unavailable,
    structured_answer_outcome,
    unavailable_structured_answer,
)
from security.log_sanitiser import sanitise_error
from ui.emojis import (
    EMOJI_BRAIN,
    EMOJI_BUILDING,
    EMOJI_CAUTION,
    EMOJI_CHART,
    EMOJI_CLIPBOARD,
    EMOJI_CROSS,
    EMOJI_SEARCH,
    EMOJI_TICK,
)


def generate_maintenance_answer(
    query: str,
    building_override: str | None = None,
    access_filter: Optional[dict[str, Any]] = None,
    *,
    _source_outcomes: list[SourceOutcome] | None = None,
    _matched_counts: list[int] | None = None,
) -> Optional[str]:
    """
    Handles maintenance queries using building-level vectors in Pinecone.
    Filters on metadata, then delegates formatting to maintenance_utils.

    IMPROVEMENTS:
    - Better error messages that distinguish different failure modes
    - Diagnostic logging to identify filtering issues
    - Pre-validation checks
    """
    logging.debug("%s Maintenance query received (%d chars)", EMOJI_SEARCH, len(query))

    # Ensure cache is ready
    BuildingCacheManager.ensure_initialised()

    # --- Parse query ---
    if not BuildingCacheManager.is_populated():
        logging.warning(
            "%s Building cache not populated — no building filtering possible",
            EMOJI_CAUTION,
        )
        known_buildings = []
    else:
        known_buildings = BuildingCacheManager.get_known_buildings()

    parsed = parse_maintenance_query(query, known_buildings=known_buildings)
    logging.info("%s PARSED: %s", EMOJI_CLIPBOARD, parsed)

    building = parsed.get("building_name")
    category = parsed.get("category")
    priority = parsed.get("priority")
    status = parsed.get("status")
    query_type = parsed.get("query_type") or "requests"

    q_l = query.lower()
    is_global_buildings_query = (
        re.search(r"\bwhich\s+buildings?\b", q_l) is not None
        or re.search(r"\ball\s+buildings?\b", q_l) is not None
        or re.search(r"\bacross\s+(all\s+)?buildings?\b", q_l) is not None
    )

    if (not building) and not is_global_buildings_query:
        # The query resolved to no known building. Before inheriting the
        # previous turn's building, check whether the user *explicitly named* a
        # building we simply failed to recognise (e.g. a garbled speech-to-text
        # name like "old dark will" for "Old Park Hill"). In that case, silently
        # substituting the previous building would confidently answer about the
        # wrong building — so ask for clarification instead. Only a genuinely
        # scope-less query (no building named) inherits conversational context.
        unresolved_building = extract_unresolved_building_phrase(query)
        if unresolved_building:
            logging.info(
                "%s Query names an unrecognised building '%s' — requesting "
                "clarification instead of inheriting previous building '%s'",
                EMOJI_CAUTION,
                unresolved_building,
                building_override,
            )
            return (
                f"{EMOJI_CROSS} I couldn't identify the building "
                f"**'{unresolved_building.title()}'** in your request.\n\n"
                "Could you confirm which building you mean? For example: "
                '*"maintenance requests at Senate House"*.'
            )

        if building_override:
            logging.info(
                "%s Using building from context override: %s",
                EMOJI_BRAIN,
                building_override,
            )
            building = building_override

    logging.info("\n🔧 MAINTENANCE QUERY ANALYSIS")
    logging.info("  Building: %s", building)
    logging.info("  Category: %s", category)
    logging.info("  Status: %s", status)
    logging.info("  Query type: %s", query_type)

    building = sanitise_building_candidate(building)

    # LOG: Final sanitised building name
    logging.info("%s SANITISED BUILDING: '%s'", EMOJI_BUILDING, building)

    # --- Namespace selection ---
    namespace = "maintenance_jobs" if query_type == "jobs" else "maintenance_requests"
    logging.info("%s Using Pinecone namespace: %s", EMOJI_SEARCH, namespace)

    matches: list[dict[str, Any]] = []
    maintenance_outcomes: list[SourceOutcome] = []
    for idx_name in TARGET_INDEXES:
        try:
            idx = open_index(idx_name)
            ns_details = idx.describe_index_stats().get("namespaces", {})
            if namespace not in ns_details:
                logging.warning(
                    "%s Namespace '%s' not found in index '%s' — available: %s",
                    EMOJI_CAUTION,
                    namespace,
                    idx_name,
                    list(ns_details.keys()),
                )
                # A missing namespace is healthy-but-empty, not an outage.
                maintenance_outcomes.append(
                    SourceOutcome(source=idx_name, status=OutcomeStatus.EMPTY)
                )
                continue

            # --- Query Pinecone for all vectors in namespace ---
            dim = idx.describe_index_stats().get("dimension", 1536)
            zero_vec = [0.0] * dim

            raw: Any = idx.query(
                vector=zero_vec,
                top_k=2000,
                namespace=namespace,
                include_metadata=True,
                filter=access_filter if access_filter else None,
            )
            response = (
                raw.to_dict() if hasattr(raw, "to_dict") else cast(dict[str, Any], raw)
            )
            index_matches = response.get("matches", [])
            matches.extend(index_matches)
            maintenance_outcomes.append(
                SourceOutcome(
                    source=idx_name,
                    status=(
                        OutcomeStatus.SUCCESS if index_matches else OutcomeStatus.EMPTY
                    ),
                    result_count=len(index_matches),
                )
            )
            logging.info(
                "%s Retrieved %d maintenance building vectors from Pinecone index '%s'",
                EMOJI_CHART,
                len(index_matches),
                idx_name,
            )
        except Exception as e:  # pylint: disable=broad-except
            logging.error(
                "Failed to query maintenance index %s: %s",
                idx_name,
                sanitise_error(e),
            )
            maintenance_outcomes.append(
                SourceOutcome(
                    source=idx_name,
                    status=OutcomeStatus.UNAVAILABLE,
                    failure=FailureInfo.from_code(
                        FailureCode.STRUCTURED_SEARCH_UNAVAILABLE,
                        STRUCTURED_RETRIEVAL_COMPONENT,
                    ),
                )
            )

    if _source_outcomes is not None:
        _source_outcomes.extend(maintenance_outcomes)

    # A total maintenance-retrieval outage must be `unavailable`, not "no data".
    raise_if_backend_unavailable(maintenance_outcomes)

    logging.info(
        "%s Retrieved %d maintenance building vectors from Pinecone (all target indexes)",
        EMOJI_CHART,
        len(matches),
    )
    matches = filter_authorized_structured_matches(
        matches,
        access_filter=access_filter,
    )
    logging.info(
        "%s Authorised maintenance vectors after ACL enforcement: %d",
        EMOJI_CHART,
        len(matches),
    )

    # DIAGNOSTIC: Check if requested building exists in the data
    if building and matches:
        building_names_in_data = set()
        for m in matches:
            md = m.get("metadata", {}) or {}
            bname = md.get("canonical_building_name") or md.get("building_name") or ""
            if bname:
                building_names_in_data.add(bname)

        # Check for exact or partial match
        building_found = any(
            building.lower() in bname.lower() or bname.lower() in building.lower()
            for bname in building_names_in_data
        )

        if building_found:
            matching_buildings = [
                bname
                for bname in building_names_in_data
                if building.lower() in bname.lower()
                or bname.lower() in building.lower()
            ]
            logging.info(
                "%s Building '%s' found in data (matches: %s)",
                EMOJI_TICK,
                building,
                matching_buildings,
            )
        else:
            logging.warning(
                "%s Building '%s' NOT found in data", EMOJI_CAUTION, building
            )
            logging.info(
                "   Available buildings sample: %s", list(building_names_in_data)[:10]
            )

    # DIAGNOSTIC: Check if requested category exists in the data
    if category and matches:
        categories_in_data = set()
        for m in matches[:100]:  # Sample first 100
            md = m.get("metadata", {}) or {}
            metrics = md.get("maintenance_metrics", {})
            if isinstance(metrics, str):
                try:
                    metrics = json.loads(metrics)
                except (json.JSONDecodeError, ValueError):
                    continue
            if isinstance(metrics, dict):
                categories_in_data.update(metrics.keys())

        category_found = any(
            category.lower() == cat.lower() for cat in categories_in_data
        )

        if category_found:
            logging.info("%s Category '%s' found in data", EMOJI_TICK, category)
        else:
            logging.warning(
                "%s Category '%s' NOT found in data sample", EMOJI_CAUTION, category
            )
            logging.info(
                "%s Available categories sample: %s",
                EMOJI_CLIPBOARD,
                list(categories_in_data)[:20],
            )

    # --- Filter by building/category/status ---
    filtered = filter_maintenance_buildings(
        matches, building, category, priority, status
    )
    if _matched_counts is not None:
        _matched_counts.append(len(filtered))
    logging.info(
        "%s Filtered matches: %d (from %d total)",
        EMOJI_SEARCH,
        len(filtered),
        len(matches),
    )

    # IMPROVED ERROR HANDLING
    if not filtered:
        # Provide specific error messages based on what we know
        if not matches:
            return f"{EMOJI_TICK} No maintenance data found in the system."

        # Check each filter independently to give better feedback
        error_parts = []

        if building:
            # Re-check if building exists without other filters
            building_only_filtered = filter_maintenance_buildings(
                matches, building, None, None, None
            )
            if not building_only_filtered:
                # Try to find similar building names
                all_buildings = set()
                for m in matches:
                    md = m.get("metadata", {}) or {}
                    bname = md.get("canonical_building_name") or md.get(
                        "building_name", ""
                    )
                    if bname:
                        all_buildings.add(bname)

                # Look for similar names
                similar = [
                    b for b in all_buildings if building.lower()[:5] in b.lower()
                ]

                if similar:
                    return (
                        f"{EMOJI_CROSS} Building **'{building}'** not found. "
                        f"Did you mean one of these?\n"
                        + "\n".join([f"  • {b}" for b in similar[:5]])
                    )
                else:
                    return f"{EMOJI_CROSS}  Building **'{building}'** not found in maintenance data."
            else:
                error_parts.append(f"at **{building}**")

        if category:
            # Re-check if category exists for this building
            category_check_filtered = filter_maintenance_buildings(
                matches, building, None, None, None
            )

            if category_check_filtered:
                # Building exists, check if it has this category
                has_category = False
                available_categories = set()

                for m in category_check_filtered:
                    md = m.get("metadata", {}) or {}
                    metrics = md.get("maintenance_metrics", {})
                    if isinstance(metrics, str):
                        try:
                            metrics = json.loads(metrics)
                        except (json.JSONDecodeError, ValueError):
                            continue
                    if isinstance(metrics, dict):
                        available_categories.update(metrics.keys())
                        if any(
                            category.lower() == cat.lower() for cat in metrics.keys()
                        ):
                            has_category = True
                            break

                if not has_category:
                    categories_list = (
                        ", ".join(sorted(available_categories)[:10])
                        if available_categories
                        else ""
                    )
                    if building:
                        msg = f"{EMOJI_CROSS} No **{category}** maintenance {query_type} found for **{building}**."
                    else:
                        msg = f"{EMOJI_CROSS} No **{category}** maintenance {query_type} found."

                    if available_categories:
                        msg += f"\n\n {EMOJI_CLIPBOARD} Available categories: {categories_list}"
                    return msg

            error_parts.append(f"**{category}**")

        if status:
            error_parts.append(f"with status **{status}**")

        if priority:
            error_parts.append(f"with priority **{priority}**")

        # Generic message
        if error_parts:
            return (
                f"{EMOJI_CROSS}  No {query_type} found " + " ".join(error_parts) + "."
            )
        else:
            return f"{EMOJI_CROSS}  No buildings match that maintenance query."

    # --- Build a deduped map: building -> status_totals ---
    building_status_map: dict[str, dict[str, int]] = {}

    for m in filtered:
        md = m.get("metadata", {}) or {}
        bname = (
            md.get("canonical_building_name")
            or md.get("building_name")
            or "Unknown building"
        )

        metrics = md.get("maintenance_metrics", {})
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except Exception:
                metrics = {}
        if not isinstance(metrics, dict) or not metrics:
            continue

        # Requests: use aggregator (status -> count)
        if query_type == "requests" and is_request_metrics(metrics):
            if category:
                agg = aggregate_request_metrics_by_category(metrics) or {}
                # find actual category key (preserve case)
                cat_key = next(
                    (
                        k
                        for k in metrics.keys()
                        if isinstance(k, str) and k.lower() == category.lower()
                    ),
                    None,
                )
                cat = (
                    (agg.get("by_category", {}) or {}).get(cat_key, {})
                    if cat_key
                    else {}
                )
                by_status = (cat or {}).get("by_status", {}) or {}
            else:
                agg = aggregate_request_metrics(metrics) or {}
                by_status = agg.get("by_status", {}) or {}

            status_totals = {
                str(k).lower(): int(v)
                for k, v in by_status.items()
                if isinstance(v, int)
            }

        else:
            # Jobs: category -> status -> count
            status_totals: dict[str, int] = {}
            for cat_name, statuses in metrics.items():
                # If category filter is specified, only process that category
                if category and cat_name.lower() != category.lower():
                    continue
                if not isinstance(statuses, dict):
                    continue
                for s, c in statuses.items():
                    if isinstance(s, str) and isinstance(c, int):
                        s_l = s.lower()
                        status_totals[s_l] = status_totals.get(s_l, 0) + c

        # Merge (max to avoid duplicate double-counting)
        if bname not in building_status_map:
            building_status_map[bname] = status_totals
        else:
            for k, v in status_totals.items():
                building_status_map[bname][k] = max(
                    building_status_map[bname].get(k, 0), v
                )

    # --- Compute totals BEFORE slicing ---
    ranked = sorted(
        ((b, sum(stats.values())) for b, stats in building_status_map.items()),
        key=lambda x: -x[1],
    )
    total_buildings = len(ranked)
    total_records = sum(total for _, total in ranked)

    logging.info(
        "%s Found %d buildings with %d total %s",
        EMOJI_TICK,
        total_buildings,
        total_records,
        query_type,
    )

    # --- Take top 10 buildings for display ---
    top_buildings = [b for b, _ in ranked[:10]]
    trimmed_stats: dict[str, dict[str, int]] = {
        b: building_status_map[b] for b in top_buildings
    }

    # ✅ Delegate the entire rendering to the multi-building formatter
    return format_multi_building_metrics(
        building_stats=trimmed_stats,
        total_buildings=total_buildings,
        total_records=total_records,
        query_type=query_type,
        limit=10,
    )


def generate_maintenance_answer_with_outcome(
    query: str,
    building_override: str | None = None,
    access_filter: Optional[dict[str, Any]] = None,
) -> StructuredAnswerOutcome:
    """Generate a maintenance answer without discarding source health."""

    source_outcomes: list[SourceOutcome] = []
    matched_counts: list[int] = []
    try:
        answer = generate_maintenance_answer(
            query,
            building_override=building_override,
            access_filter=access_filter,
            _source_outcomes=source_outcomes,
            _matched_counts=matched_counts,
        )
    except StructuredSearchUnavailable as exc:
        return unavailable_structured_answer(exc.failure, source_outcomes)
    return structured_answer_outcome(
        answer,
        source_outcomes,
        empty=bool(matched_counts) and matched_counts[-1] == 0,
    )
